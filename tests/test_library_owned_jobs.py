"""Real file/process regressions for managed hashes and direct peer transfers."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import shutil
import unittest
from unittest.mock import patch

from tests.test_library_node import LibraryCase, NODE, install, node

sys.path.insert(0, str(NODE))
import hydraweave_model_jobs as jobs
sys.path.remove(str(NODE))


@unittest.skipUnless(sys.platform.startswith("linux"), "managed node I/O uses Linux locks and atomic publication")
class OwnedModelJobsTests(LibraryCase):
    def setUp(self):
        super().setUp()
        self.destination = self.base / "destination"
        self.destination.mkdir()
        self.peer_root = install.checked_root({"root": str(self.base / "peer-install"),
                                               "lab_dir": str(self.peer_lab)}, create=True)
        self.policy = {"root": str(self.root), "lab_dir": str(self.lab)}
        self.peer = {"target": "fixture", "address": "127.0.0.1", "port": 22,
                     "host_key": "ssh-ed25519 AAAA", "tool": str(NODE / "hydraweave_model_jobs.py"),
                     "policy": {"root": str(self.peer_root), "lab_dir": str(self.peer_lab)},
                     "model_root": str(self.destination)}

    def plan(self, name="alpha"):
        return jobs.plan({**self.policy, "name": name, "model_root": str(self.models), "peer": self.peer})

    def transfer(self, plan, **kw):
        return jobs.transfer(plan, self.root / "run", lambda: None,
                             command=[sys.executable, str(NODE / "hydraweave_model_jobs.py"), "--receive"], **kw)

    def test_direct_copy_checks_every_file_and_reuses_resident_content(self):
        planned = self.plan()
        reply = self.transfer(planned)
        self.assertEqual(reply["bytes_transferred"], sum(map(len, self.alpha.values())))
        self.assertEqual(reply["bytes_reused"], 0)
        for path, expected in self.alpha.items():
            self.assertEqual((self.destination / path).read_bytes(), expected)
        verified = node.verify_unit(str(self.destination), "alpha")
        self.assertTrue(verified["ok"])
        again = self.transfer(planned)
        self.assertEqual(again["bytes_transferred"], 0)
        self.assertEqual(again["bytes_reused"], planned["bytes"])
        self.assertFalse(list((self.root / "run").glob("copy-known-hosts-*")))

    def test_single_file_copy_publishes_manifest_and_reuses(self):
        planned = self.plan("beta.gguf")
        self.assertEqual(self.transfer(planned)["bytes_transferred"], 3000)
        self.assertTrue(node.verify_unit(str(self.destination), "beta.gguf")["ok"])
        self.assertEqual(self.transfer(planned)["bytes_transferred"], 0)

    def test_existing_sidecar_refuses_before_any_model_bytes(self):
        (self.destination / "beta.gguf.sha256").write_text("unrelated manifest\n")
        progress = []
        with self.assertRaisesRegex(ValueError, "manifest already exists"):
            self.transfer(self.plan("beta.gguf"), progress=progress.append)
        self.assertEqual(progress, [])
        self.assertFalse((self.destination / "beta.gguf").exists())
        self.assertEqual((self.destination / "beta.gguf.sha256").read_text(), "unrelated manifest\n")

    def test_failed_sidecar_link_rolls_back_only_our_model_link(self):
        staged = self.base / "staging"
        staged.mkdir()
        (staged / "x.gguf").write_bytes(b"weights")
        (staged / "x.gguf.sha256").write_bytes(b"manifest")
        original = os.link
        def link(source, target, **kw):
            if source.endswith(".sha256"):
                raise OSError("injected publication failure")
            return original(source, target, **kw)
        with jobs.models.Tree(str(staged)) as source, jobs.models.Tree(str(self.destination)) as final:
            with patch.object(jobs.os, "link", side_effect=link), self.assertRaisesRegex(OSError, "injected"):
                jobs._publish_file(source, final, "x.gguf", "x.gguf.sha256")
            self.assertFalse((self.destination / "x.gguf").exists())
            self.assertTrue((staged / "x.gguf").exists())
            jobs._publish_file(source, final, "x.gguf", "x.gguf.sha256")
        self.assertEqual((self.destination / "x.gguf").read_bytes(), b"weights")

    @unittest.skipUnless(shutil.which("ssh"), "SSH option parser required")
    def test_known_hosts_path_with_spaces_is_parsed_as_one_path(self):
        path = self.base / "known hosts"
        result = subprocess.run(["ssh", "-G", "-F", "/dev/null", "-o", "UserKnownHostsFile=" + jobs._ssh_path(path), "127.0.0.1"],
                                capture_output=True, text=True, timeout=5, check=True)
        value = next(line for line in result.stdout.splitlines() if line.startswith("userknownhostsfile "))
        self.assertEqual(value.removeprefix("userknownhostsfile "), str(path))

    def test_retained_verified_staging_files_are_not_retransmitted(self):
        planned = self.plan()
        expected = {r["path"]: r["sha256"] for r in planned["files"]}
        stage_id = hashlib.sha256(install.canonical({"name": "alpha", "files": expected})).hexdigest()[:24]
        stage = self.destination / (".hydraweave-incoming-" + stage_id)
        (stage / "alpha").mkdir(parents=True)
        (stage / "alpha/a.bin").write_bytes(self.alpha["alpha/a.bin"])
        result = self.transfer(planned)
        self.assertEqual(result["bytes_reused"], 5000)
        self.assertEqual(result["bytes_transferred"], planned["bytes"] - 5000)

    def test_existing_mismatch_is_never_overwritten(self):
        planned = self.plan()
        self.transfer(planned)
        (self.destination / "alpha/a.bin").write_bytes(b"bad" * 100)
        with self.assertRaisesRegex(ValueError, "integrity|size"):
            self.transfer(planned)
        self.assertEqual((self.destination / "alpha/a.bin").read_bytes(), b"bad" * 100)

    def test_changed_source_and_incomplete_manifest_fail_before_publication(self):
        planned = self.plan()
        (self.models / "alpha/a.bin").write_bytes(b"C" * 5000)
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.transfer(planned)
        self.assertFalse((self.destination / "alpha").exists())
        with self.assertRaisesRegex(ValueError, "complete"):
            self.plan("delta")

    def test_destination_symlink_cannot_redirect_staging_writes(self):
        planned = self.plan()
        outside = self.base / "outside"
        outside.mkdir()
        (self.destination / "alpha").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.transfer(planned)
        self.assertEqual(list(outside.iterdir()), [])

    def test_owner_gate_excludes_second_job_and_engine_admission(self):
        policy = jobs.models.effective_policy(self.policy)
        with jobs.storage_owner(policy):
            with self.assertRaisesRegex(ValueError, "owns this node"):
                jobs.refuse_storage_owner(self.root)
            with self.assertRaisesRegex(ValueError, "owns this node"):
                with jobs.storage_owner(policy):
                    self.fail("second owner entered")
        jobs.refuse_storage_owner(self.root)

    def test_managed_verification_process_and_poll(self):
        reply = node.run("model-verify", {**self.policy, "model_root": str(self.models), "name": "alpha"})
        self.assertNotIn("error", reply)
        end = time.monotonic() + 10
        while time.monotonic() < end:
            state = node.run("copy-job", {**self.policy, "job": reply["job"], "renew": True})
            if state.get("state") not in ("queued", "running"):
                break
            time.sleep(.05)
        self.assertEqual(state["state"], "succeeded", state)
        self.assertTrue(state["result"]["ok"])
        self.assertGreater(state["result"]["bytes_hashed"], 0)

    def test_expired_owner_cancels_without_reading_weights(self):
        job = "a" * 32
        paths = jobs._paths(self.root, job)
        spec = {"kind": "verify", "job": job, "payload": {**self.policy, "name": "alpha", "model_root": str(self.models)},
                "plan": None, "started": time.time()}
        install.atomic_json(paths["spec"], spec)
        paths["owner"].touch()
        os.utime(paths["owner"], (time.time() - 200, time.time() - 200))
        with patch.object(jobs.models, "verify_unit", side_effect=AssertionError("weight read")):
            self.assertEqual(jobs.job_main(str(paths["spec"])), 1)
        self.assertEqual(install.read_json(paths["state"])["state"], "cancelled")

    def test_a_blocked_peer_does_not_block_cancellation(self):
        started = time.monotonic()
        def check():
            if time.monotonic() - started > .25:
                raise jobs.models.Stopped("cancelled")
        with self.assertRaises(jobs.models.Stopped):
            jobs.transfer(self.plan(), self.root / "run", check,
                          command=[sys.executable, "-c", "import time; time.sleep(60)"])
        self.assertLess(time.monotonic() - started, 5)

    def test_next_disk_block_is_prefetched_while_current_block_is_sent(self):
        body = b"A" * (2 * jobs.models.BLOCK)
        (self.models / "beta.gguf").write_bytes(body)
        (self.models / "beta.gguf.sha256").write_text(hashlib.sha256(body).hexdigest() + "  beta.gguf\n")
        read_ahead = threading.Event()
        original_read, original_send = jobs.models.Reader.read_at, jobs.Wire.send
        def read(reader, offset):
            if offset >= jobs.models.BLOCK:
                read_ahead.set()
            return original_read(reader, offset)
        def send(wire, data):
            if len(data) >= jobs.models.BLOCK:
                self.assertTrue(read_ahead.wait(2), "next disk read must start before current network send finishes")
            return original_send(wire, data)
        with patch.object(jobs.models.Reader, "read_at", read), patch.object(jobs.Wire, "send", send):
            result = self.transfer(self.plan("beta.gguf"))
        self.assertEqual(result["bytes_transferred"], len(body))

    def test_missing_destination_space_refuses_before_reading_payload(self):
        from types import SimpleNamespace
        planned = self.plan()
        document = {**planned, "model_root": str(self.destination), "policy": self.peer["policy"]}
        class NoPayload:
            check = staticmethod(lambda: None)
            def json(self, value):
                raise AssertionError("requested payload despite no destination space")
            def read(self, count):
                raise AssertionError("read payload despite no destination space")
        with patch.object(jobs.os, "fstatvfs", return_value=SimpleNamespace(f_bavail=0, f_frsize=4096)), \
                self.assertRaisesRegex(ValueError, "insufficient destination space"):
            jobs.receive(NoPayload(), document)
        self.assertFalse((self.destination / "alpha").exists())

    def test_writer_rejects_root_swap_to_a_symlink_after_validation(self):
        outside = self.base / "outside"
        outside.mkdir()
        original = jobs.models.read_root
        def validated(value):
            checked = original(value)
            if str(value) == str(self.destination):
                self.destination.rename(self.base / "old-destination")
                self.destination.symlink_to(outside, target_is_directory=True)
            return checked
        with patch.object(jobs.models, "read_root", side_effect=validated), self.assertRaises(OSError):
            jobs.WriteTree(self.destination)
        self.assertEqual(list(outside.iterdir()), [])

    def test_malformed_resident_completion_cannot_report_success(self):
        for value in ({"state": "failed"}, {"state": "succeeded", "files": 1, "destination": "wrong", "bytes_transferred": 0, "bytes_reused": 0}):
            script = "import sys; sys.stdin.readline(); print(" + repr(json.dumps({"complete": value})) + ", flush=True)"
            with self.subTest(value), self.assertRaisesRegex(ValueError, "peer"):
                jobs.transfer(self.plan(), self.root / "run", lambda: None,
                              command=[sys.executable, "-c", script])


if __name__ == "__main__":
    unittest.main()
