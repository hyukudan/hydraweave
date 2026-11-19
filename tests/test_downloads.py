"""Confirmed pinned downloads: synthetic curl, temp roots, no network or ssh."""
import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from hydraweave import downloads
from hydraweave.config import parse
from tests.test_config import example


NODE = Path(__file__).resolve().parents[1] / "node"
FAKE_CURL = Path(__file__).resolve().parent / "fixtures" / "node_download" / "fake_curl.py"
sys.path.insert(0, str(NODE))
spec = importlib.util.spec_from_file_location("node_download_under_test", NODE / "hydraweave_download.py")
node = importlib.util.module_from_spec(spec)
spec.loader.exec_module(node)
sys.path.remove(str(NODE))
install = node.install
BODY = b"payload"


def fixture_job(spec_path):
    # The worker's I/O algorithms remain portable unit tests. Managed ownership
    # is a Linux node contract and is exercised by the real entry point there.
    return (node.job_main if os.name == "posix" else node._job_main)(str(spec_path))


class NodeDownloadCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hydraweave-download-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "install root"
        install.checked_root({"root": str(self.root)}, create=True)
        self.assets = self.base / "assets"
        self.assets.mkdir()
        (self.assets / node.ASSET_MARKER).write_text("", encoding="utf-8")
        self.lab = self.base / "lab"
        (self.lab / ".gpu-turn").mkdir(parents=True)
        which = patch.object(node.shutil, "which", side_effect=lambda name, path=None:
                             "/usr/bin/curl" if name == "curl" else None)
        which.start()
        self.addCleanup(which.stop)

    def item(self, **changes):
        item = {"repo": "unsloth/Qwen3.8-Flash-Next-GGUF", "revision": "a" * 40, "file": "UD/part-1.gguf",
                "bytes": len(BODY), "sha256": hashlib.sha256(BODY).hexdigest(),
                "directory": str(self.assets), "destination": str(self.assets / "part-1.gguf")}
        item.update(changes)
        return item

    def payload(self, *items, **extra):
        return {"root": str(self.root), "lab_dir": str(self.lab), "files": list(items) or [self.item()], **extra}


class NodePlanTests(NodeDownloadCase):
    @unittest.skipUnless(os.name == "posix", "node directory preparation uses POSIX directory descriptors")
    def test_prepare_marks_only_empty_directories_without_following_links(self):
        directory = self.base / "new-models"
        result = node.prepare_directory(self.root, {"directory": str(directory), "lab_dir": str(self.lab)})
        self.assertTrue(result["prepared"])
        self.assertTrue((directory / node.ASSET_MARKER).is_file())
        self.assertTrue(node.prepare_directory(self.root, {"directory": str(directory)})["existing"])
        unrelated = self.base / "unmarked"
        unrelated.mkdir()
        (unrelated / "important").write_bytes(b"keep")
        with self.assertRaisesRegex(ValueError, "nonempty unmarked"):
            node.prepare_directory(self.root, {"directory": str(unrelated)})
        self.assertEqual(list(unrelated.iterdir()), [unrelated / "important"])
    def test_merge_retry_reserves_only_missing_input_and_output(self):
        first = self.item()
        second = self.item(file="UD/part-2.gguf", destination=str(self.assets / "part-2.gguf"))
        merge = {"kind": "gguf-split", "destination": str(self.assets / "merged.gguf"),
                 "expected": {"bytes": 13, "sha256": "c" * 64}}
        # The retained input is rehashed by the job; it does not need room to download again.
        Path(first["destination"]).write_bytes(BODY)
        missing = len(BODY)
        free = node.HEADROOM + missing + merge["expected"]["bytes"]
        usage = node.shutil.disk_usage(self.assets)._replace(free=free)
        with patch.object(node.shutil, "disk_usage", return_value=usage):
            result = node.plan(self.payload(first, second, merge=merge), self.root)
        self.assertEqual(result["disk"][0]["required_bytes"], missing + 13)
        self.assertTrue(result["disk"][0]["ok"])
        self.assertEqual(Path(first["destination"]).read_bytes(), BODY)

    def test_split_merge_plan_space_pin_confirmation_and_paths(self):
        first = self.item()
        second = self.item(file="UD/part-2.gguf", destination=str(self.assets / "part-2.gguf"))
        merge = {"kind": "gguf-split", "destination": str(self.assets / "merged.gguf"), "expected": None}
        payload = self.payload(first, second, merge=merge)
        result = node.plan(payload, self.root)
        assert result["merge"] == merge
        assert result["disk"][0]["required_bytes"] == len(BODY) * 4
        pin = {"bytes": 123, "sha256": "c" * 64}
        pinned = node.plan({**payload, "merge": {**merge, "expected": pin}}, self.root)
        assert result["confirm"] != pinned["confirm"]
        assert pinned["disk"][0]["required_bytes"] == len(BODY) * 2 + 123
        for change in ({"destination": str(self.base / "escape.gguf")}, {"destination": first["destination"]},
                       {"kind": "command"}, {"expected": {"bytes": 1, "sha256": "invalid"}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                node.plan({**payload, "merge": {**merge, **change}}, self.root)
        Path(merge["destination"] + ".partial").touch()
        with self.assertRaisesRegex(ValueError, "partial already exists"):
            node.plan(payload, self.root)

    def test_plan_is_exact_and_deterministic(self):
        first = node.run("download-plan", self.payload())
        self.assertNotIn("error", first)
        self.assertEqual(first["confirm"], node.run("download-plan", self.payload())["confirm"])
        row = first["files"][0]
        url = "https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF/resolve/" + "a" * 40 + "/UD/part-1.gguf"
        self.assertEqual(row["argv"][:3], ["nice", "-n", "19"])
        self.assertEqual(row["argv"][-3:], ["--output", str(self.assets / ".part-1.gguf.hydraweave-part"), url])
        self.assertIn("--proto", row["argv"])
        self.assertTrue(first["disk"][0]["ok"])
        other = node.run("download-plan", self.payload(self.item(sha256="b" * 64)))
        self.assertNotEqual(first["confirm"], other["confirm"])

    def test_plan_refusals(self):
        cases = {
            "revision": self.item(revision="main"),
            "repo": self.item(repo="../x"),
            "file traversal": self.item(file="../secret"),
            "no sha": self.item(sha256=None),
            "zero bytes": self.item(bytes=0),
            "escape": self.item(destination=str(self.base / "elsewhere.gguf")),
            "dot name": self.item(destination=str(self.assets / "..")),
            "missing dir": self.item(directory=str(self.base / "none"), destination=str(self.base / "none" / "x")),
            "root overlap": self.item(directory=str(self.root), destination=str(self.root / "x.gguf")),
            "lab overlap": self.item(directory=str(self.lab), destination=str(self.lab / "x.gguf")),
        }
        for name, item in cases.items():
            with self.subTest(name):
                self.assertIn("error", node.run("download-plan", self.payload(item)))
        (self.assets / "part-1.gguf").write_bytes(b"existing")
        reply = node.run("download-plan", self.payload())
        self.assertNotIn("error", reply)  # a job verifies and reuses it, never overwrites it
        self.assertEqual(reply["disk"][0]["required_bytes"], 0)
        self.assertEqual((self.assets / "part-1.gguf").read_bytes(), b"existing")

    def test_only_a_marked_hydraweave_asset_directory(self):
        # A lab model directory (no marker) is never written into, even outside lab_dir.
        lab_models = self.base / "lab models"
        lab_models.mkdir()
        reply = node.run("download-plan", self.payload(self.item(directory=str(lab_models),
                                                                 destination=str(lab_models / "part-1.gguf"))))
        self.assertIn("not dedicated to hydraweave downloads", reply["error"])
        (lab_models / node.ASSET_MARKER).mkdir()
        reply = node.run("download-plan", self.payload(self.item(directory=str(lab_models),
                                                                 destination=str(lab_models / "part-1.gguf"))))
        self.assertIn("not dedicated to hydraweave downloads", reply["error"])

    def test_active_lease_refuses_download(self):
        code = node.run("download-plan", self.payload())["confirm"]
        install.atomic_json(self.root / "run" / "lease-l1.json", {"lease": "l1", "state": "acquired"})
        with patch.object(node.subprocess, "Popen") as popen:
            reply = node.run("download", self.payload(confirm=code))
        self.assertIn("lease is active: l1", reply["error"])
        popen.assert_not_called()

    def test_stale_running_job_is_settled(self):
        path = self.root / "run" / "job-abc.json"
        install.atomic_json(path, {"job": "abc", "kind": "download", "state": "running", "boot_id": "an-earlier-boot"})
        self.assertEqual(node.run("download-cancel", {"root": str(self.root), "job": "abc"})["state"], "failed")
        self.assertFalse((self.root / "run" / "job-abc.stop").exists())
        state = install.read_json(path)
        self.assertIn("rebooted", state["error"])
        reply = install.run("job", {"root": str(self.root), "job": "abc"})
        self.assertEqual(reply["state"], "failed")
        # Jobs without a boot record (builds, hashing) are reported as they are.
        install.atomic_json(path, {"job": "abc", "kind": "install", "state": "running"})
        self.assertEqual(install.run("job", {"root": str(self.root), "job": "abc"})["state"], "running")

    def test_curl_missing(self):
        with patch.object(node.shutil, "which", return_value=None):
            self.assertIn("curl", node.run("download-plan", self.payload())["error"])

    def test_confirm_mismatch_and_pair_turn_refused(self):
        code = node.run("download-plan", self.payload())["confirm"]
        with patch.object(node.subprocess, "Popen") as popen:
            reply = node.run("download", self.payload(confirm="0" * 12))
            self.assertIn("does not match", reply["error"])
            (self.lab / ".gpu-turn" / "holder").write_text("PAIR sx-1 123\n")
            reply = node.run("download", self.payload(confirm=code))
            self.assertIn("PAIR", reply["error"])
            (self.lab / ".gpu-turn" / "holder").write_text("SINGLE sx-2 456\n")
            reply = node.run("download", self.payload(confirm=code))
            self.assertIn("GPU turn is held: SINGLE", reply["error"])
            popen.assert_not_called()

    def test_cancel_only_download_jobs(self):
        install.atomic_json(self.root / "run" / "job-abc.json", {"job": "abc", "kind": "install", "state": "running"})
        self.assertIn("not a download", node.run("download-cancel", {"root": str(self.root), "job": "abc"})["error"])
        install.atomic_json(self.root / "run" / "job-abc.json", {"job": "abc", "kind": "download", "state": "running",
                                                                "boot_id": install.boot_id(), "pid": os.getpid()})
        self.assertEqual(node.run("download-cancel", {"root": str(self.root), "job": "abc"})["state"], "running")
        self.assertTrue((self.root / "run" / "job-abc.stop").exists())


class NodeJobTests(NodeDownloadCase):
    def retained_job(self):
        first = self.item()
        second = self.item(file="UD/part-2.gguf", destination=str(self.assets / "part-2.gguf"))
        action = {"kind": "gguf-split", "destination": str(self.assets / "merged.gguf"), "expected": None}
        for item in (first, second):
            Path(item["destination"]).write_bytes(BODY)
        planned = node.plan(self.payload(first, second, merge=action), self.root)
        spec_path = self.root / "run" / "job-retained.spec.json"
        install.atomic_json(spec_path, {"root": str(self.root), "job": "retained", "lab_dir": str(self.lab),
                                       "files": planned["files"], "merge": action})
        return spec_path, planned

    def test_retained_part_hash_observes_cancel_and_gpu_ownership(self):
        for event in ("cancel", "turn", "lease"):
            with self.subTest(event=event):
                spec_path, planned = self.retained_job()
                checked = []
                original = install.hash_file

                def hasher(path, check=None):
                    checked.append(path)
                    if event == "cancel":
                        (self.root / "run" / "job-retained.stop").touch()
                    elif event == "turn":
                        (self.lab / ".gpu-turn" / "holder").write_text("SINGLE other 456\n")
                    else:
                        install.atomic_json(self.root / "run" / "lease-other.json",
                                            {"lease": "other", "state": "acquired"})
                    return original(path, check=check)

                with patch.object(install, "hash_file", side_effect=hasher), \
                        patch.object(node.subprocess, "Popen") as spawn:
                    code = fixture_job(spec_path)
                state = install.read_json(self.root / "run" / "job-retained.json")
                self.assertEqual(code, 1)
                self.assertEqual(state["state"], "cancelled" if event == "cancel" else "failed")
                self.assertEqual(len(checked), 1, "stop before hashing another retained model part")
                self.assertIn({"cancel": "cancelled", "turn": "GPU turn", "lease": "lease"}[event], state["error"])
                self.assertFalse(Path(planned["merge"]["destination"]).exists())
                self.assertTrue(all(Path(row["destination"]).read_bytes() == BODY for row in planned["files"]))
                spawn.assert_not_called()
                (self.lab / ".gpu-turn" / "holder").unlink(missing_ok=True)
                (self.root / "run" / "lease-other.json").unlink(missing_ok=True)

    def test_pre_cancelled_fetch_does_not_spawn_or_create_partial(self):
        row = node.plan(self.payload(), self.root)["files"][0]
        with patch.object(node.subprocess, "Popen") as spawn:
            self.assertEqual(node.fetch(row, lambda: "cancelled", lambda: None), "cancelled")
        spawn.assert_not_called()
        self.assertFalse(Path(row["part"]).exists())
        self.assertFalse(Path(row["destination"]).exists())

    @unittest.skipUnless(os.name == "posix", "process groups")
    def test_cancellation_kills_a_child_that_ignores_sigterm(self):
        row = node.plan(self.payload(), self.root)["files"][0]
        ready = self.assets / "child-ready"
        row["argv"] = [sys.executable, "-c",
                       "import signal,time,sys; from pathlib import Path; "
                       "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                       "Path(sys.argv[1]).write_bytes(b'pay'); Path(sys.argv[2]).touch(); time.sleep(30)",
                       row["part"], str(ready)]
        created = []
        popen = node.subprocess.Popen

        def spawn(*args, **kwargs):
            child = popen(*args, **kwargs)
            created.append(child)
            return child

        start = time.monotonic()
        try:
            with patch.object(node, "POLL_S", .01), patch.object(node, "STOP_GRACE_S", .1), \
                    patch.object(node.subprocess, "Popen", side_effect=spawn):
                result = node.fetch(row, lambda: "cancelled" if ready.exists() else None, lambda: None)
            self.assertEqual(result, "cancelled")
            self.assertLess(time.monotonic() - start, 5)
            self.assertIsNotNone(created[0].poll())
            self.assertEqual(Path(row["part"]).read_bytes(), b"pay")
            self.assertFalse(Path(row["destination"]).exists())
            # Cancellation releases the writer lock, so a later job can resume.
            lock = node.lock_part(Path(row["part"]))
            os.close(lock)
        finally:
            for child in created:
                if child.poll() is None:
                    os.killpg(child.pid, node.signal.SIGKILL)
                    child.wait(timeout=5)

    def test_cancellation_after_hash_keeps_verified_partial_unpublished(self):
        original = install.hash_file
        row = node.plan(self.payload(), self.root)["files"][0]
        cancelled = []

        def hasher(path, check=None):
            digest = original(path, check=check)
            cancelled.append(True)
            return digest

        row["argv"] = [sys.executable, str(FAKE_CURL), *row["argv"][row["argv"].index("/usr/bin/curl") + 1:]]
        with patch.object(install, "hash_file", side_effect=hasher), patch.object(node, "POLL_S", .01):
            result = node.fetch(row, lambda: "cancelled" if cancelled else None, lambda: None)
        self.assertEqual(result, "cancelled")
        self.assertEqual(Path(row["part"]).read_bytes(), BODY)
        self.assertFalse(Path(row["destination"]).exists())

    def job(self, body=BODY, **env):
        rows = node.plan(self.payload(), self.root)["files"]
        for row in rows:
            row["argv"] = [sys.executable, str(FAKE_CURL), *row["argv"][row["argv"].index("/usr/bin/curl") + 1:]]
        spec_path = self.root / "run" / "job-j1.spec.json"
        install.atomic_json(spec_path, {"root": str(self.root), "job": "j1", "lab_dir": str(self.lab), "files": rows})
        with patch.dict(os.environ, {"FAKE_BODY": body.decode(), **env}):
            code = fixture_job(spec_path)
        return code, install.read_json(self.root / "run" / "job-j1.json")

    def test_verified_file_is_renamed(self):
        code, state = self.job()
        self.assertEqual((code, state["state"]), (0, "succeeded"))
        self.assertEqual((self.assets / "part-1.gguf").read_bytes(), BODY)
        self.assertFalse((self.assets / ".part-1.gguf.hydraweave-part").exists())

    def test_verified_resident_file_is_reused_without_curl(self):
        (self.assets / "part-1.gguf").write_bytes(BODY)
        code, state = self.job(FAKE_EXIT="99")
        self.assertEqual((code, state["state"]), (0, "succeeded"))
        self.assertEqual((state["bytes_transferred"], state["bytes_reused"]), (0, len(BODY)))
        self.assertEqual(state["verified"][str(self.assets / "part-1.gguf")]["sha256"], hashlib.sha256(BODY).hexdigest())

    def test_corrupt_resident_file_is_not_overwritten(self):
        (self.assets / "part-1.gguf").write_bytes(b"corrupt")
        code, state = self.job()
        self.assertEqual((code, state["state"]), (1, "failed"))
        self.assertEqual((self.assets / "part-1.gguf").read_bytes(), b"corrupt")

    def test_resident_merged_file_does_not_download_parts_again(self):
        destination = self.assets / "merged.gguf"
        destination.write_bytes(BODY)
        second = self.item(file="UD/part-2.gguf", destination=str(self.assets / "part-2.gguf"))
        action = {"kind": "gguf-split", "destination": str(destination), "expected": {"bytes": len(BODY), "sha256": hashlib.sha256(BODY).hexdigest()}}
        plan = node.plan(self.payload(self.item(), second, merge=action), self.root)
        self.assertEqual(plan["disk"][0]["required_bytes"], 0)
        spec_path = self.root / "run" / "job-j1.spec.json"
        install.atomic_json(spec_path, {"root": str(self.root), "job": "j1", "lab_dir": str(self.lab), "files": plan["files"], "merge": action})
        with patch.object(subprocess, "Popen", side_effect=AssertionError("redownloaded parts")):
            self.assertEqual(fixture_job(spec_path), 0)
        state = install.read_json(self.root / "run" / "job-j1.json")
        self.assertEqual(state["bytes_transferred"], 0)
        self.assertEqual(state["bytes_reused"], len(BODY))

    def test_merge_checks_owned_job_expiry_and_keeps_verified_parts(self):
        from types import SimpleNamespace
        first, second = self.item(), self.item(file="UD/part-2.gguf", destination=str(self.assets / "part-2.gguf"))
        for row in (first, second):
            Path(row["destination"]).write_bytes(BODY)
        action = {"kind": "gguf-split", "destination": str(self.assets / "merged.gguf"), "expected": {"bytes": 3, "sha256": "a" * 64}}
        plan = node.plan(self.payload(first, second, merge=action), self.root)
        spec_path = self.root / "run" / "job-j1.spec.json"
        owner = self.root / "run" / "job-j1.owner"
        owner.touch()
        install.atomic_json(spec_path, {"root": str(self.root), "job": "j1", "lab_dir": str(self.lab), "files": plan["files"], "merge": action, "owner_ttl": 120})
        def merge(*args, check, **kw):
            os.utime(owner, (time.time() - 200, time.time() - 200))
            check()
            raise AssertionError("merge continued after owner expiry")
        with patch.dict(sys.modules, {"hydraweave_gguf_merge": SimpleNamespace(merge=merge)}):
            self.assertEqual(fixture_job(spec_path), 1)
        state = install.read_json(self.root / "run" / "job-j1.json")
        self.assertEqual(state["state"], "cancelled")
        self.assertTrue(all(Path(row["destination"]).exists() for row in (first, second)))

    def test_verify_failure_leaves_no_final_file(self):
        code, state = self.job(body=b"tampered")
        self.assertEqual((code, state["state"]), (1, "failed"))
        self.assertIn("verification failed", state["error"])
        self.assertEqual(sorted(p.name for p in self.assets.iterdir()), [node.ASSET_MARKER])

    def test_failed_command_keeps_partial_for_resume(self):
        code, state = self.job(body=b"pay", FAKE_EXIT="22")
        self.assertIn("exited 22", state["error"])
        self.assertFalse((self.assets / "part-1.gguf").exists())
        self.assertEqual((self.assets / ".part-1.gguf.hydraweave-part").read_bytes(), b"pay")

    def test_hash_uses_the_direct_hasher_and_stops_for_a_turn(self):
        calls = []
        original = install.hash_file

        def hasher(path, check=None):
            calls.append(path)
            (self.lab / ".gpu-turn" / "holder").write_text("SINGLE sx-2 456\n")
            return original(path, check=check)

        with patch.object(install, "hash_file", side_effect=hasher):
            code, state = self.job()
        self.assertEqual(calls, [self.assets / ".part-1.gguf.hydraweave-part"])
        self.assertIn("GPU turn is held", state["error"])
        self.assertFalse((self.assets / "part-1.gguf").exists())
        self.assertEqual((self.assets / ".part-1.gguf.hydraweave-part").read_bytes(), BODY)

    @unittest.skipUnless(os.name == "posix", "flock")
    def test_partial_file_has_one_writer(self):
        part = self.assets / ".part-1.gguf.hydraweave-part"
        held = node.lock_part(part)
        try:
            code, state = self.job()
        finally:
            os.close(held)
        self.assertIn("another download job is writing", state["error"])
        self.assertFalse((self.assets / "part-1.gguf").exists())

    @unittest.skipUnless(os.name == "posix", "process groups")
    def test_pair_turn_interrupts_running_download(self):
        (self.lab / ".gpu-turn" / "holder").write_text("PAIR sx-1 123\n")
        code, state = self.job(FAKE_SLEEP="30")
        self.assertIn("PAIR", state["error"])
        self.assertFalse((self.assets / "part-1.gguf").exists())


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.obj = example()
        for machine in self.obj["machines"][1:]:
            machine["lab_dir"] = "/srv/lab"
        self.obj["assets"]["halo-1"]["model"].update(bytes=111, sha256="c" * 64)
        self.calls = []

        def call(client, verb, payload, timeout=60):
            self.calls.append((verb, payload))
            return {"files": [], "disk": [], "confirm": "0123456789ab", "job": "j", "log": "/l"}

        for name, value in (("_client", lambda machine: machine), ("_call", call)):
            patcher = patch.object(downloads.orchestrate, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_catalog_is_checked_at_the_engine_pin(self):
        lock = json.loads((Path(__file__).resolve().parents[1] / "versions.lock").read_text(encoding="utf-8"))
        self.assertEqual(downloads.catalog()["engine_sha"], lock["components"]["engine"]["commit"])
        with patch.object(downloads, "read_json", side_effect=lambda path: (
                dict(json.loads(Path(path).read_text(encoding="utf-8")), engine_sha="0" * 40)
                if Path(path).name == "sources.json" else json.loads(Path(path).read_text(encoding="utf-8")))):
            with self.assertRaisesRegex(downloads.ConfigError, "pinned engine"):
                downloads.catalog()

    def test_catalog_pins_and_reasons(self):
        rows = downloads.sources(parse(self.obj), "qwen-replicas")
        self.assertEqual(rows["model"]["revision"], "38bb39ee97821de2c9009abb7e93950eec396e66")
        self.assertEqual(len(rows["model"]["files"]), 4)
        self.assertEqual(rows["model"]["merge"], "gguf-split")
        self.assertNotIn("after", rows["model"])
        self.assertEqual(rows["sidecar"]["status"], "unavailable")
        pair = downloads.sources(parse(self.obj), "ds41-pair")
        self.assertTrue(all(row["status"] == "unavailable" for row in pair.values()))

    def test_plan_targets_the_asset_directory(self):
        result = downloads.plan(parse(self.obj), "qwen-replicas", "model", "halo-1")
        verb, payload = self.calls[-1]
        self.assertEqual(verb, "download-plan")
        self.assertEqual({row["directory"] for row in payload["files"]}, {"/opt/assets"})
        self.assertTrue(all(row["destination"].startswith("/opt/assets/Qwen3.8") for row in payload["files"]))
        self.assertIn("--confirm 0123456789ab", result["next"])

    def test_refusals_before_any_node_call(self):
        cluster = parse(self.obj)
        for args, message in ((("qwen-replicas", "sidecar", "halo-1"), "not downloadable"),
                              (("qwen-replicas", "model", "halo-2"), "real bytes and sha256"),
                              (("qwen-replicas", "model", "workstation"), "not a machine"),
                              (("ds41-pair", "ds41_model", "halo-1"), "not downloadable")):
            with self.subTest(args), self.assertRaisesRegex(ValueError, message):
                downloads.plan(cluster, *args)
        with self.assertRaisesRegex(ValueError, "12-character"):
            downloads.start(cluster, "qwen-replicas", "model", "halo-1", "yes")
        self.assertEqual(self.calls, [])

    def test_start_sends_the_code(self):
        downloads.start(parse(self.obj), "qwen-replicas", "model", "halo-1", "0123456789ab")
        self.assertEqual(self.calls[-1][0], "download")
        self.assertEqual(self.calls[-1][1]["confirm"], "0123456789ab")


if __name__ == "__main__":
    unittest.main()
