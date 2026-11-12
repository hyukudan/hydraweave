"""Atomic store writes, log setup and the transport factory (stdlib unittest, runs under both runners)."""
import logging
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from hydraweave import store
from hydraweave.log import setup


class StoreTransportLogTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)

    def test_atomic_write_retries_permission_error(self):
        target = self.tmp / "state.json"
        real_replace = store.os.replace
        calls = []

        def replace(source, destination):
            calls.append(1)
            if len(calls) == 1:
                raise PermissionError("sharing")
            return real_replace(source, destination)

        with mock.patch.object(store.os, "replace", replace):
            store.write_json(target, {"ok": True}, mode=0o600)
        self.assertEqual(store.read_json(target), {"ok": True})
        self.assertEqual(len(calls), 2)

    def test_atomic_write_keeps_existing_file_after_replace_failure(self):
        target = self.tmp / "state.json"
        target.write_text('{"old": true}\n', encoding="utf-8")
        with mock.patch.object(store.os, "replace", side_effect=PermissionError("busy")):
            with self.assertRaises(PermissionError):
                store.write_json(target, {"new": True})
        self.assertEqual(store.read_json(target), {"old": True})

    def test_log_setup_redacts_token_values_by_not_logging_them(self):
        path = self.tmp / "hydraweave.log"
        root = logging.getLogger("hydraweave")
        before = list(root.handlers)
        setup("DEBUG", json=True, file=path)

        def restore():
            for handler in root.handlers[:]:
                if handler not in before:
                    root.removeHandler(handler)
                    handler.close()
        self.addCleanup(restore)
        logging.getLogger("hydraweave.test").info("authorization: Bearer token-secret")
        root.handlers[-1].flush()
        output = path.read_text(encoding="utf-8")
        self.assertNotIn("token-secret", output)
        self.assertIn('"logger": "hydraweave.test"', output)

    def test_transport_factory_selects_agent(self):
        from hydraweave import transport

        class AgentClient:
            def __init__(self, machine, path):
                self.machine, self.path = machine, path

        machine = type("Machine", (), {"access": "agent", "id": "node1"})()
        with mock.patch("hydraweave.onboarding.Client", AgentClient):
            client = transport.for_machine(machine, self.tmp / "config.json")
        self.assertIsInstance(client, AgentClient)
        self.assertEqual(client.path, self.tmp / "config.json")


if __name__ == "__main__":
    unittest.main()
