"""Configuration binding, cancellation and independent node progress."""
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from hydraweave import downloads, library_jobs
from hydraweave.config import parse
from hydraweave.telemetry.store import Store
from hydraweave.web.api import JobContext
from hydraweave.web.features import library
from tests.test_config import example


class ModelCoordinationTests(unittest.TestCase):
    def test_copy_confirmation_binds_source_transport_and_destination(self):
        cluster = parse(example())
        arguments = {"machine": "halo-1", "model_root": "/opt/assets", "name": "model",
                     "destination": "halo-2", "destination_root": "/opt/assets"}
        planned = {"files": [{"path": "model/a.bin", "bytes": 4, "sha256": "a" * 64}], "bytes": 4, "confirm": "b" * 16}
        with patch.object(library_jobs, "peer", return_value={}), \
                patch.object(downloads, "_policy", return_value={}), \
                patch.object(library_jobs, "_call", return_value=planned) as called:
            reviewed = library_jobs.copy_plan(cluster, None, arguments)
            raw = example()
            raw["machines"][1]["http_address"] = "192.0.2.99"
            with closing(Store(":memory:")) as store, self.assertRaisesRegex(ValueError, "configuration changed"):
                library_jobs.execute(parse(raw), store, "/unused/config", {**arguments, "confirm": reviewed["confirm"]}, JobContext(60), "copy")
        self.assertTrue(all(call.args[1] == "copy-plan" for call in called.call_args_list))

    def test_status_failure_stops_renewal_and_requests_cancel_until_owner_expiry(self):
        clock = [0.0]
        renews, cancelled = [], []
        def poll(renew):
            renews.append(renew)
            clock[0] += 80
            raise OSError("offline")
        with patch.object(library_jobs.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(library_jobs.time, "sleep", return_value=None):
            ctx = JobContext(1000)
            with self.assertRaises(library_jobs.CleanupUnresolved):
                library_jobs.watch_owned(ctx, lambda state: None, poll, lambda: cancelled.append(True))
        self.assertTrue(renews[0])
        self.assertTrue(all(not value for value in renews[1:]))
        self.assertGreaterEqual(len(cancelled), 2)
        self.assertGreaterEqual(clock[0], 155)

    def test_local_verification_uses_cache_free_reader_and_ledger_without_node_calls(self):
        with tempfile.TemporaryDirectory() as temporary, closing(Store(":memory:")) as store:
            base = Path(temporary)
            root = base / "models"
            root.mkdir()
            body = b"fixture weights"
            (root / "test.gguf").write_bytes(body)
            (root / "test.gguf.sha256").write_text(hashlib.sha256(body).hexdigest() + "  test.gguf\n")
            raw = example()
            raw["machines"][0].update(os="windows" if os.name == "nt" else "linux",
                                      install_root=str(base / "install"), model_roots=[str(root)])
            raw["assets"]["workstation"] = {}
            cluster = parse(raw)
            args = {"machine": "workstation", "model_root": str(root), "name": "test.gguf"}
            with patch.object(downloads, "_node", side_effect=AssertionError("node contacted")), \
                    patch("hydraweave.paths.state_dir", return_value=base / "state"):
                result = library_jobs.execute(cluster, store, base / "config.json", args, JobContext(60), "verify")
            self.assertTrue(result["ok"])
            self.assertEqual(result["bytes_hashed"], len(body))
            saved = store.get("library/verify", "workstation|" + str(root) + "|test.gguf")
            self.assertEqual(saved["state"], "succeeded")

    def test_independent_library_scans_overlap(self):
        from types import SimpleNamespace
        barrier = threading.Barrier(2)
        with closing(Store(":memory:")) as store:
            api = SimpleNamespace(cluster=lambda: parse(example()))
            def inventory(cluster, mid, store):
                barrier.wait(timeout=2)
                return {"machine": mid, "roots": []}
            with patch.object(library.telemetry, "open_store", return_value=store), \
                    patch.object(downloads, "inventory", side_effect=inventory):
                result = library.scan(api, {"machines": ["halo-1", "halo-2"]}, JobContext(10))
            self.assertEqual(result["machines"], {"halo-1": "scanned", "halo-2": "scanned"})


if __name__ == "__main__":
    unittest.main()
