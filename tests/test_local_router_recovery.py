"""Crash recovery uses only fixture processes and loopback listeners, never engines."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hydraweave import cli, orchestrate as orch, procs


@unittest.skipUnless(sys.platform == "linux" and hasattr(os, "pidfd_open"), "Linux process receipts and pidfds")
class _RouterCrashFixture:
    standalone = False

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="hydraweave router recovery ")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.key = self.directory / "replica.key"
        self.key.write_text("fixture-key")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.endpoint = {"address": "127.0.0.1", "port": probe.getsockname()[1]}
        self.router = {"directory": str(self.directory), "endpoint": self.endpoint}
        self.spec = self.directory / "spec.json"
        orch._write(self.spec, {"rendered": {"listen": self.endpoint}, "keys": [str(self.key)],
                               "deadline": time.time() + 60})
        self.state_file = self.directory / "state.json"
        if self.standalone:
            self.spec = self.directory / ("router-" + "a" * 32 + ".json")
            orch._write(self.spec, {"listen": self.endpoint})
            self.state_file = self.directory / "router-state.json"
        child = self.directory / "fixture-router.py"
        child.write_text('''import json, socket, sys, time
from pathlib import Path
config = Path(sys.argv[1])
endpoint = json.loads(config.read_text())["listen"]
with socket.socket() as listener:
    listener.bind((endpoint["address"], endpoint["port"]))
    listener.listen()
    config.with_name("listening").touch()
    while True:
        time.sleep(0.1)
''')
        owner = '''import sys
from pathlib import Path
from hydraweave import orchestrate as orch
original = orch.procs.LocalChild
def child(argv, env, directory, name, **kwargs):
    # Exercise recovery of legacy children that predate the Linux lifetime guard.
    return original([sys.executable, sys.argv[2], argv[-1]], env, directory, name, parent_guard=False, **kwargs)
def status(endpoint):
    if not Path(sys.argv[1]).with_name("listening").exists():
        raise OSError("fixture not listening yet")
    return {"schema": "tensormeld/replica-router-status-v1"}
orch.procs.LocalChild = child
orch._http_json = status
'''
        if self.standalone:
            owner += '''from types import SimpleNamespace
from hydraweave import cli
cli._get_json = lambda endpoint, path: status(endpoint)
cli._router_run(SimpleNamespace(directory=str(Path(sys.argv[1]).parent), rendered=sys.argv[1], token="a" * 32))
'''
        else:
            owner += "orch.local_router_run(sys.argv[1])\n"
        with (self.directory / "owner.log").open("wb") as log:
            self.owner = subprocess.Popen([sys.executable, "-c", owner, str(self.spec), str(child)],
                                          stdout=log, stderr=log)
        self.identity = None
        self.addCleanup(self.cleanup_processes)
        end = time.monotonic() + 8
        while time.monotonic() < end:
            state = self.state_file
            record = orch._read(state) if state.exists() else {}
            if record.get("status" if self.standalone else "state") == ("running" if self.standalone else "ready"):
                self.pid = orch._read(state)["pid"]
                self.identity = procs.process_identity(self.pid)
                break
            if self.owner.poll() is not None:
                self.fail((self.directory / "owner.log").read_text())
            time.sleep(0.02)
        else:
            self.fail("fixture router did not become ready")

    def cleanup_processes(self):
        if self.owner.poll() is None:
            self.owner.kill()
        self.owner.wait(timeout=5)
        state = self.state_file
        record = orch._read(state) if state.exists() else {}
        identity = self.identity or record.get("process")
        if identity and procs.process_identity(identity["pid"]) == identity:
            try:
                os.killpg(identity["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass

    def kill_owner(self):
        self.owner.kill()
        self.owner.wait(timeout=5)
        self.assertEqual(procs.process_identity(self.pid), self.identity)
        with self.assertRaises(procs.ProcessError):
            procs.check_port(**self.endpoint)


class RouterCrashRecoveryTests(_RouterCrashFixture, unittest.TestCase):
    def test_hard_killed_helper_router_is_stopped_before_ownership_cleanup(self):
        self.kill_owner()
        orch._stop_local_router(self.router)
        procs.check_port(**self.endpoint)
        self.assertEqual(orch._read(self.directory / "state.json"), {"state": "stopped"})
        self.assertFalse(self.key.exists())
        self.assertFalse(self.spec.exists())

    def test_changed_process_identity_never_signals_pid_or_drops_keys(self):
        self.kill_owner()
        record = orch._read(self.directory / "state.json")
        record["process"] = dict(self.identity, start_ticks=self.identity["start_ticks"] + 1)
        orch._write(self.directory / "state.json", record)
        with self.assertRaisesRegex(orch.OrchestrationError, "identity changed"):
            orch._stop_local_router(self.router)
        self.assertEqual(procs.process_identity(self.pid), self.identity)
        self.assertTrue(self.key.exists())
        self.assertTrue(self.spec.exists())

    def test_legacy_receipt_without_identity_keeps_ownership(self):
        self.kill_owner()
        orch._write(self.directory / "state.json", {"state": "ready", "pid": self.pid})
        with self.assertRaisesRegex(orch.OrchestrationError, "identity is incomplete"):
            orch._stop_local_router(self.router)
        self.assertEqual(procs.process_identity(self.pid), self.identity)
        self.assertTrue(self.key.exists())

    def test_config_mismatch_never_signals_an_unrelated_process(self):
        self.kill_owner()
        with self.assertRaisesRegex(procs.ProcessError, "command does not match"):
            procs.stop_owned_group(self.identity, argument=str(self.directory / "other.json"), grace_s=0)
        self.assertEqual(procs.process_identity(self.pid), self.identity)


class StandaloneRouterCrashRecoveryTests(_RouterCrashFixture, unittest.TestCase):
    standalone = True

    def test_cli_recovers_hard_killed_helper_and_does_not_report_false_stopped(self):
        self.kill_owner()
        with patch.object(cli, "_runtime", return_value=self.directory), patch.object(cli, "_json") as output:
            self.assertEqual(cli.main(["router", "status"]), 0)
            self.assertEqual(output.call_args.args[0]["status"], "recovery_required")
            cli._router_stop()
            self.assertEqual(output.call_args.args[0]["status"], "stopped")
        procs.check_port(**self.endpoint)
        self.assertEqual(orch._read(self.state_file)["status"], "stopped")
        self.assertFalse(self.spec.exists())

    def test_cli_recovery_refuses_changed_identity(self):
        self.kill_owner()
        record = orch._read(self.state_file)
        record["process"] = dict(self.identity, start_ticks=self.identity["start_ticks"] + 1)
        orch._write(self.state_file, record)
        with patch.object(cli, "_runtime", return_value=self.directory):
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                cli._router_stop()
        self.assertEqual(procs.process_identity(self.pid), self.identity)
        self.assertTrue(self.spec.exists())


class RouterStopFailureTests(unittest.TestCase):
    def test_failed_child_stop_keeps_plaintext_and_ownership_for_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            key = directory / "replica.key"
            key.write_text("fixture-key")
            spec = directory / "spec.json"
            orch._write(spec, {"rendered": {"listen": {"address": "127.0.0.1", "port": 1}},
                               "keys": [str(key)], "deadline": time.time() - 1})
            child = Mock()
            child.process.pid = 987654321
            child.stop.side_effect = OSError("fixture stop failed")
            with patch.object(orch.procs, "LocalChild", return_value=child), \
                    patch.object(orch, "_http_json", side_effect=OSError), patch.object(orch.signal, "signal"):
                with self.assertRaisesRegex(OSError, "fixture stop failed"):
                    orch.local_router_run(spec)
            self.assertTrue(key.exists())
            self.assertTrue(spec.exists())
            self.assertTrue((directory / "router.json").exists())
            self.assertNotEqual(orch._read(directory / "state.json")["state"], "stopped")

    def test_cli_failed_child_stop_does_not_publish_stopped_or_remove_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            token = "a" * 32
            rendered = directory / ("router-" + token + ".json")
            orch._write(rendered, {"listen": {"address": "127.0.0.1", "port": 1}})
            child = Mock()
            child.process.pid = 987654321
            child.alive.return_value = False
            child.stop.side_effect = OSError("fixture stop failed")
            with patch.object(procs, "LocalChild", return_value=child), patch.object(cli, "_router_env", return_value={}), \
                    patch.object(cli.signal, "signal"):
                with self.assertRaisesRegex(OSError, "fixture stop failed"):
                    cli._router_run(SimpleNamespace(directory=str(directory), rendered=str(rendered), token=token))
            self.assertTrue(rendered.exists())
            self.assertEqual(orch._read(directory / "router-state.json")["status"], "stopping")


@unittest.skipUnless(sys.platform == "linux" and hasattr(os, "pidfd_open"), "Linux process receipts and pidfds")
class OwnedGroupRecoveryTests(unittest.TestCase):
    def test_recovery_stops_a_reviewed_descendant_as_well_as_its_leader(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "owned-config.json"
            receipt = Path(temporary) / "descendant.pid"
            code = '''import signal, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
def stop(*args):
    child.wait(timeout=5)
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
Path(sys.argv[2]).write_text(str(child.pid))
while True:
    time.sleep(0.1)
'''
            leader = subprocess.Popen([sys.executable, "-c", code, str(config), str(receipt)], start_new_session=True)
            identity = procs.process_identity(leader.pid)
            try:
                end = time.monotonic() + 5
                while not receipt.exists() and time.monotonic() < end:
                    time.sleep(0.01)
                self.assertTrue(receipt.exists())
                child_pid = int(receipt.read_text())
                self.assertEqual(procs.process_identity(child_pid)["pgid"], leader.pid)
                procs.stop_owned_group(identity, argument=str(config), grace_s=1)
                leader.wait(timeout=5)
                self.assertIsNone(procs.process_identity(child_pid))
            finally:
                if leader.poll() is None:
                    os.killpg(leader.pid, signal.SIGKILL)
                    leader.wait(timeout=5)

    def test_term_ignoring_owned_process_is_killed_after_grace(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "owned-config.json"
            ready = Path(temporary) / "ready"
            code = '''import signal, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[2]).touch()
time.sleep(60)
'''
            child = subprocess.Popen([sys.executable, "-c", code, str(config), str(ready)], start_new_session=True)
            try:
                end = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < end:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                procs.stop_owned_group(procs.process_identity(child.pid), argument=str(config), grace_s=0.02)
                self.assertEqual(child.wait(timeout=5), -signal.SIGKILL)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
