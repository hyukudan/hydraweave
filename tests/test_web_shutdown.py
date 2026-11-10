"""Portal shutdown reaps owned feature work without stopping detached CLI jobs."""
import os
from pathlib import Path
import shlex
import shutil
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from hydraweave import admission, components
from hydraweave.web import features, server
from hydraweave.web.api import API, APIError, JobContext
from hydraweave.web.features import components as component_web


class ShutdownTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="hydraweave-shutdown-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.api = API(self.root / "cluster.json", audit_path=self.root / "audit.jsonl")
        self.api.admission_db = admission.Admission(":memory:")
        self.addCleanup(self.api.admission_db.close)
        self.registry = features.Registry()
        features.reset(self.registry)
        self.addCleanup(features.reset)
        self.portal = server.create_server(0, api=self.api)
        self.addCleanup(self.portal.server_close)

    def wait_for(self, predicate, message, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail(message)

    @unittest.skipUnless(os.name != "nt" and shutil.which("git"), "POSIX Git process groups")
    def test_close_cancels_download_child_and_removes_unpublished_stage(self):
        # Actual Git -> Python transport -> Python child, all in Git's own process
        # group. Only the fetch is replaced, so no network or real node is needed.
        worker = self.root / "transport.py"
        child_pid = self.root / "child.pid"
        child_stopped = self.root / "child.stopped"
        group_pid = self.root / "group.pid"
        worker.write_text(
            "import os, pathlib, signal, subprocess, sys, time\n"
            "root = pathlib.Path(__file__).parent\n"
            "if len(sys.argv) > 1:\n"
            "    def stop(*_):\n"
            "        (root / 'child.stopped').write_text('terminated')\n"
            "        raise SystemExit(0)\n"
            "    signal.signal(signal.SIGTERM, stop)\n"
            "    (root / 'child.pid').write_text(str(os.getpid()))\n"
            "else:\n"
            "    child = subprocess.Popen([sys.executable, __file__, 'child'])\n"
            "    def stop(*_):\n"
            "        child.wait(timeout=3)\n"
            "        raise SystemExit(0)\n"
            "    signal.signal(signal.SIGTERM, stop)\n"
            "    (root / 'group.pid').write_text(str(os.getpgrp()))\n"
            "while True: time.sleep(0.05)\n", encoding="utf-8")

        def cleanup_group():
            if group_pid.exists():
                try:
                    os.killpg(int(group_pid.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.addCleanup(cleanup_group)
        alias = "!" + shlex.join([sys.executable, str(worker)])

        def runner(arguments, **kwargs):
            if arguments[0] == "fetch":
                arguments = ["-c", "alias.test-component-fetch=" + alias, "test-component-fetch"]
            return components.git(arguments, **kwargs)

        commit = "a" * 40
        manager = components.Manager(self.api.config_path, root=self.root / "cache", runner=runner,
                                     versions={"components": {"engine": {
                                         "repo": components.BY_ID["engine"].repo, "commit": commit}}})
        component_web.register(self.registry)
        with mock.patch.object(component_web, "manager", return_value=manager):
            job = self.api.mutate("components/download", {"component": "engine", "commit": commit})
            self.wait_for(lambda: child_pid.exists() and group_pid.exists(), "fetch child did not start")
            self.assertEqual(len(list((manager.root / "engine").glob(".download-*"))), 1)
            started = time.monotonic()
            self.portal.server_close()
            self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(self.api._jobs[job["id"]]["state"], "cancelled")
        self.assertEqual(self.api._features_running, {})
        self.assertTrue(child_stopped.exists(), "the transport descendant must receive termination")
        self.assertFalse((manager.root / "engine" / commit).exists())
        self.assertEqual(list((manager.root / "engine").glob(".download-*")), [])
        if Path("/proc").exists():
            self.wait_for(lambda: not Path("/proc", child_pid.read_text(), "stat").exists(),
                          "fetch descendant was not reaped")

    def test_close_preserves_real_detached_core_children(self):
        for action in ("up", "install"):
            with self.subTest(action=action):
                ready = self.root / (action + ".ready")
                release = self.root / (action + ".release")
                program = ("import json, os, pathlib, time; "
                           f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid())); "
                           f"gate = pathlib.Path({str(release)!r}); "
                           "exec('while not gate.exists(): time.sleep(0.01)'); "
                           "print(json.dumps({'finished': True}))")
                results, errors = [], []

                def run():
                    try:
                        results.append(self.api._run_detached(action, {}))
                    except Exception as exc:
                        errors.append(exc)

                with mock.patch.object(self.api, "_cli_argv", return_value=[sys.executable, "-c", program]):
                    thread = threading.Thread(target=run, daemon=True)
                    thread.start()
                    try:
                        self.wait_for(ready.exists, "detached CLI child did not start")
                        self.portal.server_close()
                        self.assertTrue(thread.is_alive(), "closing the dashboard must not stop the CLI child")
                        self.assertEqual(errors, [])
                    finally:
                        release.touch()
                        thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(results, [{"finished": True}])

    def test_close_wait_is_bounded_and_leaves_cleanup_ownership_with_worker(self):
        context = JobContext(60)
        self.api._features_running["slow"] = context
        self.api._holds["slow"] = "still-owned"
        try:
            started = time.monotonic()
            self.assertEqual(self.api.close_features(timeout_s=0.05), 1)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertTrue(context.cancelled())
            self.assertEqual(context.reason, "portal is closing")
            self.assertEqual(self.api._holds, {"slow": "still-owned"})
        finally:
            self.api._features_running.clear()
            self.api._holds.clear()

    def test_closed_portal_refuses_before_validation(self):
        validate, run = mock.Mock(), mock.Mock()
        self.registry.job("demo/start", run, validate=validate)
        self.portal.server_close()
        with self.assertRaises(APIError) as caught:
            self.api.mutate("demo/start", {})
        self.assertEqual(caught.exception.status, 503)
        validate.assert_not_called()
        run.assert_not_called()

    def test_shutdown_during_admission_rolls_back_without_starting_job(self):
        entered, proceed = threading.Event(), threading.Event()
        run, rollback = mock.Mock(), mock.Mock()
        self.registry.job("demo/start", run, validate=lambda api, a: a, rollback=rollback,
                          exclusive=True, resources=lambda api, a: {"node"})
        original_hold = self.api._hold
        errors = []

        def held(*args):
            hold = original_hold(*args)
            entered.set()
            proceed.wait(timeout=5)
            return hold

        def request():
            try:
                self.api.mutate("demo/start", {})
            except Exception as exc:
                errors.append(exc)

        with mock.patch.object(self.api, "_hold", side_effect=held):
            thread = threading.Thread(target=request, daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(timeout=5))
                self.portal.server_close()
            finally:
                proceed.set()
                thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], APIError)
        self.assertEqual(errors[0].status, 503)
        self.assertEqual((self.api._holds, self.api._active, self.api._features_running), ({}, None, {}))
        run.assert_not_called()
        rollback.assert_called_once_with(self.api, {})


if __name__ == "__main__":
    unittest.main()
