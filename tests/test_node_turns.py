"""Hardware-free tests for the built-in turn provider."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("turn_provider_test", ROOT / "node" / "hydraweave_turn_provider.py")
provider_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provider_module)
LEASE_SPEC = importlib.util.spec_from_file_location("turn_lease_test", ROOT / "node" / "hydraweave_lease.py")
lease = importlib.util.module_from_spec(LEASE_SPEC)
LEASE_SPEC.loader.exec_module(lease)


class BuiltinTurnTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "flock provider is POSIX-only")
    def test_acquire_holder_and_release(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = provider_module.BuiltinProvider(temp)
            ticket = manager.acquire("hl-test")
            self.assertEqual(manager.holder()["label"], "hl-test")
            manager.release(ticket)
            self.assertIsNone(manager.holder())

    def test_validate_single_without_lab_dir(self):
        with tempfile.TemporaryDirectory() as temp:
            payload = {"root": temp, "label": "hl-test", "kind": "single",
                   "processes": [{"name": "engine", "machine": "local", "argv": [sys.executable],
                                  "env": {}, "ready": "qwen-stderr",
                                  "health_url": "http://127.0.0.1:18501/health"}]}
            self.assertIsNone(lease.validate_spec(payload).get("lab_dir"))

    @unittest.skipUnless(os.name == "posix", "flock provider is POSIX-only")
    def test_dead_waiter_does_not_block_queue(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = provider_module.BuiltinProvider(temp)
            stale = manager.directory / "00000000000000000001-dead"
            stale.write_text("999999999\n")
            manager.release(manager.acquire("hl-test"))
            self.assertFalse(stale.exists())

    def test_pair_without_lab_dir_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            payload = {"root": temp, "label": "hl-test", "kind": "pair",
                       "processes": [{"name": "engine", "machine": "local", "argv": [sys.executable],
                                      "env": {}, "ready": "qwen-stderr",
                                      "health_url": "http://127.0.0.1:18501/health"}]}
            with self.assertRaises(ValueError):
                lease.validate_spec(payload)


if __name__ == "__main__":
    unittest.main()
