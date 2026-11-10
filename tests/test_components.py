"""Real local Git fixtures: checks, staged sources, reuse, cancellation and pin provenance."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from hydraweave import components as core, config, engines
from hydraweave.web.features import components as web
from hydraweave.web.api import JobFailed
from hydraweave.winproc import no_window


def local_git(args, cwd=None):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=20,
                          **no_window()).stdout


@unittest.skipUnless(shutil.which("git"), "Git is required for source download fixtures")
class ComponentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.source = self.base / "origin"
        self.source.mkdir()
        local_git(["init", "--quiet", "--initial-branch=main", "--template="], self.source)
        local_git(["config", "user.name", "Component fixture"], self.source)
        local_git(["config", "user.email", "fixture@example.invalid"], self.source)
        (self.source / "engine.txt").write_text("pinned engine\n")
        local_git(["add", "engine.txt"], self.source)
        local_git(["commit", "--quiet", "-m", "pinned"], self.source)
        self.pin = local_git(["rev-parse", "HEAD"], self.source).strip()
        (self.source / "engine.txt").write_text("new upstream engine\n")
        local_git(["commit", "--quiet", "-am", "new upstream"], self.source)
        self.head = local_git(["rev-parse", "HEAD"], self.source).strip()
        self.versions = {"components": {"engine": {"repo": core.BY_ID["engine"].repo, "commit": self.pin}}}
        self.calls = []
        self.now = 100000.0
        self.manager = core.Manager(self.base / "config" / "cluster.json", root=self.base / "cache",
                                    runner=self.runner, clock=lambda: self.now, versions=self.versions)

    def runner(self, args, *, cwd=None, cancelled=lambda: False, timeout=45):
        self.calls.append(list(args))
        if cancelled():
            raise core.Cancelled("cancelled")
        args = list(args)
        if args[0] == "ls-remote":
            args[-2] = self.source.as_uri()
        if args[0] == "fetch":
            args[args.index("origin")] = self.source.as_uri()
        return local_git(["-c", "protocol.file.allow=always", *args], cwd)

    def test_inventory_is_passive_and_preserves_unknown_state(self):
        result = self.manager.state()
        self.assertEqual(len(result["components"]), 4)
        engine = result["components"][0]
        self.assertEqual(engine["pins"][0]["commit"], self.pin)
        self.assertIsNone(engine["upstream"])
        self.assertEqual(engine["active"], [])
        self.assertIsNone(engine["update_available"])
        self.assertFalse(self.calls)
        self.assertFalse(self.manager.root.exists())

    def test_check_reads_remote_head_without_updating_any_checkout(self):
        (self.source / "engine.txt").write_text("uncommitted work\n")
        before = local_git(["status", "--porcelain"], self.source)
        result = self.manager.check("engine")
        self.assertEqual(result["checked"]["engine"]["commit"], self.head)
        self.assertEqual(result["checked"]["engine"]["branch"], "main")
        self.assertFalse(result["installed"])
        self.assertEqual([call[0] for call in self.calls], ["ls-remote"])
        self.assertEqual(before, local_git(["status", "--porcelain"], self.source))
        self.assertFalse((self.manager.root / "engine").exists())
        self.assertEqual(self.versions["components"]["engine"]["commit"], self.pin)
        self.assertTrue(self.manager.state()["components"][0]["update_available"])

    def test_pinned_download_is_verified_and_atomic_and_not_an_install(self):
        result = self.manager.download("engine", self.pin)
        checkout = Path(result["path"])
        self.assertEqual((checkout / "engine.txt").read_text(), "pinned engine\n")
        self.assertEqual(local_git(["rev-parse", "HEAD"], checkout).strip(), self.pin)
        self.assertEqual(result["state"], "staged")
        self.assertFalse(result["installed"])
        self.assertFalse(result["submodules_fetched"])
        self.assertEqual(list(checkout.parent.parent.glob(".download-*")), [])
        self.assertEqual(self.versions["components"]["engine"]["commit"], self.pin)
        self.assertEqual(self.manager.state()["components"][0]["downloads"][0]["commit"], self.pin)

    def test_upstream_revision_requires_recent_successful_check(self):
        with self.assertRaises(PermissionError):
            self.manager.download("engine", self.head)
        self.assertFalse(self.calls)
        self.manager.check("engine")
        result = self.manager.download("engine", self.head)
        self.assertEqual(result["selection"], "upstream-check")
        self.now += core.CHECK_MAX_AGE_S + 1
        with self.assertRaises(PermissionError):
            self.manager.download("engine", self.head)

    def test_existing_clean_source_is_reused_without_fetch(self):
        result = self.manager.download("engine", self.pin)
        self.calls.clear()
        repeated = self.manager.download("engine", self.pin)
        self.assertTrue(repeated["reused"])
        self.assertEqual(repeated["path"], result["path"])
        self.assertNotIn("fetch", [c[0] for c in self.calls])

    def test_dirty_checkout_is_preserved_and_never_reset(self):
        result = self.manager.download("engine", self.pin)
        checkout = Path(result["path"])
        (checkout / "engine.txt").write_text("local changes\n")
        (checkout / "notes.txt").write_text("local untracked notes\n")
        with self.assertRaisesRegex(core.ComponentError, "modified"):
            self.manager.download("engine", self.pin)
        self.assertEqual((checkout / "engine.txt").read_text(), "local changes\n")
        self.assertEqual((checkout / "notes.txt").read_text(), "local untracked notes\n")
        self.assertNotIn("reset", [c[0] for c in self.calls])

    def test_changed_origin_is_preserved_and_refused(self):
        result = self.manager.download("engine", self.pin)
        checkout = Path(result["path"])
        local_git(["remote", "set-url", "origin", "https://example.invalid/another"], checkout)
        with self.assertRaisesRegex(core.ComponentError, "another revision"):
            self.manager.download("engine", self.pin)
        self.assertEqual(local_git(["remote", "get-url", "origin"], checkout).strip(), "https://example.invalid/another")

    def test_wrong_fetched_commit_never_publishes_a_source(self):
        actual = self.manager.runner
        self.manager.runner = lambda args, **kw: (self.head + "\n" if args[0] == "rev-parse" else actual(args, **kw))
        with self.assertRaisesRegex(core.ComponentError, "does not match"):
            self.manager.download("engine", self.pin)
        self.assertEqual(self.manager.downloads("engine"), [])
        self.assertEqual(list((self.manager.root / "engine").glob(".download-*")), [])

    def test_cancelled_download_cleans_only_its_stage(self):
        actual = self.manager.runner
        cancel = False

        def runner(args, **kwargs):
            nonlocal cancel
            result = actual(args, **kwargs)
            if args[0] == "fetch":
                cancel = True
            return result
        self.manager.runner = runner
        with self.assertRaises(core.Cancelled):
            self.manager.download("engine", self.pin, cancelled=lambda: cancel)
        self.assertFalse((self.manager.root / "engine" / self.pin).exists())
        self.assertEqual(list((self.manager.root / "engine").glob(".download-*")), [])
        self.assertEqual(local_git(["rev-parse", "HEAD"], self.source).strip(), self.head)

    def test_failed_check_is_recorded_and_invalidates_old_upstream_permission(self):
        self.manager.check("engine")
        self.manager.runner = mock.Mock(side_effect=core.ComponentError("network unavailable"))
        result = self.manager.check("engine")
        self.assertEqual(result["errors"], 1)
        row = self.manager.state()["components"][0]
        self.assertIsNone(row["upstream"]["commit"])
        self.assertIn("network unavailable", row["upstream"]["error"])
        with self.assertRaises(PermissionError):
            self.manager.allowed("engine", self.head)
        self.assertEqual(self.manager.allowed("engine", self.pin), "configured-pin")

    def test_arbitrary_revisions_repositories_and_paths_are_rejected(self):
        for cid, commit in (("../engine", self.pin), ("engine", "main"), ("engine", "--upload-pack=x"),
                            ("engine", "a" * 40), ("https://example.invalid/x", self.pin)):
            with self.subTest(cid=cid, commit=commit), self.assertRaises((ValueError, PermissionError)):
                self.manager.download(cid, commit)
        self.assertFalse(self.calls)
        bad = deepcopy(self.versions)
        bad["components"]["engine"]["repo"] = "https://example.invalid/untrusted"
        with self.assertRaisesRegex(core.ComponentError, "does not match"):
            core.catalog(versions=bad)

    def test_unmarked_nonempty_cache_is_not_adopted(self):
        self.manager.root.mkdir()
        unrelated = self.manager.root / "keep.txt"
        unrelated.write_text("not a component cache")
        with self.assertRaisesRegex(core.ComponentError, "not managed"):
            self.manager.check("engine")
        self.assertEqual(unrelated.read_text(), "not a component cache")
        self.assertFalse(self.calls)

    def test_another_process_cannot_download_into_the_same_component(self):
        self.manager._ensure()
        directory = self.manager.root / "engine"
        directory.mkdir()
        code = ("import sys; from pathlib import Path; from hydraweave.components import _lock\n"
                "with _lock(Path(sys.argv[1])):\n"
                " print('locked', flush=True)\n"
                " sys.stdin.readline()\n")
        process = subprocess.Popen([sys.executable, "-u", "-c", code, str(directory / ".download.lock")],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, cwd=Path(__file__).resolve().parents[1], **no_window())
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            with self.assertRaisesRegex(PermissionError, "another process"):
                self.manager.download("engine", self.pin)
            self.assertFalse(self.calls)
            self.assertFalse((directory / self.pin).exists())
        finally:
            process.communicate("\n", timeout=10)
        self.assertEqual(process.returncode, 0)

    def test_driver_has_no_automatic_activation_or_invented_qualification(self):
        self.manager.check("usb4-driver")
        result = self.manager.download("usb4-driver", self.head)
        row = next(r for r in self.manager.state()["components"] if r["id"] == "usb4-driver")
        self.assertEqual(row["pins"], [])
        self.assertFalse(row["activation"]["automatic"])
        self.assertFalse(row["upstream_is_qualified"])
        self.assertFalse(result["installed"])
        self.assertTrue(all(c[0] in {"ls-remote", "init", "remote", "fetch", "rev-parse", "checkout", "status"}
                            for c in self.calls))

    def test_symlink_cache_and_source_are_refused(self):
        external = self.base / "external"
        external.mkdir()
        try:
            self.manager.root.symlink_to(external, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaisesRegex(core.ComponentError, "symlinks"):
            self.manager.download("engine", self.pin)
        self.assertEqual(list(external.iterdir()), [])

    def test_saved_launch_evidence_is_explicitly_not_a_live_read(self):
        result = self.manager.state(launches={"my-profile": {"nodes": ["halo-1", "halo-2"], "phase": "running",
                                                            "launch": {"components": {"engine": self.pin}}}})
        active = result["components"][0]["active"][0]
        self.assertEqual(active["commit"], self.pin)
        self.assertEqual(active["evidence"], "saved-launch")
        self.assertFalse(active["live_verified"])
        self.assertFalse(self.calls)


class CatalogAndWebTests(unittest.TestCase):
    def cluster(self):
        return SimpleNamespace(profiles={"code": {"preset": "qwen-replicas-w5", "nodes": ["a"]},
                                         "reason": {"preset": "ds41-pair-w5", "rank0": "a", "rank1": "b"}},
                               machines={"a": SimpleNamespace(access="ssh"), "b": SimpleNamespace(access="ssh")})

    def test_catalog_respects_each_preset_pin(self):
        rows, profiles = core.catalog(self.cluster())
        engine = next(r for r in rows if r["id"] == "engine")
        self.assertEqual(len(engine["pins"]), 3)
        for name, preset in (("code", "qwen-replicas-w5"), ("reason", "ds41-pair-w5")):
            pin = next(p for p in engine["pins"] if name in p["profiles"])
            self.assertEqual(pin["commit"], config.preset(preset)["components"]["engine"]["commit"])
            self.assertTrue(pin["qualified"])
        self.assertEqual(profiles[1]["machines"], ["a", "b"])

    def test_plan_is_the_pinned_install_on_the_build_node(self):
        plan = core.install_plan(self.cluster(), "reason")
        self.assertEqual(plan["build_on"], "a")
        self.assertEqual(plan["machines"], ["a", "b"])
        self.assertEqual(plan["action"]["endpoint"], "install")
        self.assertEqual(plan["action"]["arguments"], {"profile": "reason"})
        self.assertFalse(plan["driver"]["automatic"])
        self.assertFalse(plan["starts_inference"])

    def test_first_run_inventory_works_without_cluster_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            api = SimpleNamespace(config_path=Path(temp) / "cluster.json", clock=lambda: 1000,
                                  cluster=mock.Mock(side_effect=ValueError("configuration is missing")),
                                  document=lambda name, **value: {"schema": name, **value})
            with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(core, "cache_root",
                                                                               return_value=Path(temp) / "components"):
                result = web.overview(api, {})
                self.assertEqual(len(result["components"]), 4)
                self.assertIn("configuration is missing", result["config_error"])
                self.assertEqual(result["profiles"], [])
                self.assertEqual(list(Path(temp).iterdir()), [])
                plan = web.plan(api, {})
                self.assertEqual(plan["plans"], [])

    def test_unknown_web_fields_are_refused(self):
        api = SimpleNamespace()
        for fn, arguments in ((web.overview, {"path": "/tmp/x"}), (web.plan, {"command": "x"}),
                              (web.check_validate, {"repo": "https://example.invalid"}),
                              (web.download_validate, {"component": "engine", "commit": "a" * 40, "path": "/tmp"})):
            with self.subTest(fn=fn), self.assertRaises(ValueError):
                fn(api, arguments)

    def test_check_failure_is_a_failed_job_not_a_successful_update(self):
        fake = SimpleNamespace(check=mock.Mock(return_value={"errors": 1, "checked": {}}))
        ctx = SimpleNamespace(cancelled=lambda: False)
        with mock.patch.object(web, "manager", return_value=fake), self.assertRaises(JobFailed):
            web.check(None, {}, ctx)

    def test_cache_path_cannot_be_supplied_by_browser(self):
        with mock.patch.dict(os.environ, {"HYDRAWEAVE_COMPONENT_CACHE": "../shared"}):
            with self.assertRaisesRegex(core.ComponentError, "absolute"):
                core.Manager("/tmp/cluster.json")

    @unittest.skipUnless(os.name != "nt" and shutil.which("git"), "POSIX Git process groups")
    def test_git_timeout_kills_its_child_process_group(self):
        started = time.monotonic()
        with self.assertRaisesRegex(core.ComponentError, "timed out"):
            core.git(["-c", "alias.component-test-wait=!sleep 20", "component-test-wait"], timeout=0.1)
        self.assertLess(time.monotonic() - started, 5)


if __name__ == "__main__":
    unittest.main()
