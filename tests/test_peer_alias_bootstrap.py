"""Append-only peer routes can finish onboarding without weakening identity, launch or version guards."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest
from unittest import mock

from hydraweave import procs
from hydraweave.web.api import APIError
from hydraweave.web.features import ops
from tests.test_ops_ops import Base, RAW


class PeerAliasBootstrapTests(Base, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.raw = deepcopy(RAW)
        self.raw["machines"].append({**self.raw["machines"][2], "id": "halo-3", "http_address": "192.0.2.12",
                                     "ssh_target": "halo-3"})
        self.raw["machines"][1]["peer_ssh"] = {"halo-2": "existing-peer"}
        self.config.write_text(json.dumps(self.raw), encoding="utf-8")
        self.api._cached = {"profiles": {"profiles": []}}
        self.api.orchestrate._state_path = lambda profile: Path(self.dir.name) / ("up-" + profile + ".json")
        self.replies["status"] = {"error": "install root is not marked"}

    def arguments(self, **changes):
        entry = deepcopy(self.raw["machines"][1])
        entry["peer_ssh"]["halo-3"] = "new-peer"
        entry.update(changes)
        return {"op": "edit", "id": "halo-1", "machine": entry,
                "version": hashlib.sha256(self.config.read_bytes()).hexdigest()}

    def save(self, args):
        return self.api.feature_post("ops/config/save", args)[1]

    def validate(self, args):
        return self.api.feature_post("ops/config/validate", args)[1]

    def test_get_exposes_only_the_specific_permission_without_polling(self):
        with mock.patch.object(self.api, "snapshot", side_effect=AssertionError("no fresh snapshot")):
            doc = self.api.read("ops/config")
        rows = {row["id"]: row for row in doc["machines"]}
        self.assertTrue(rows["halo-1"]["frozen"])
        self.assertTrue(rows["halo-1"]["add_peer_aliases"])
        self.assertFalse(rows["workstation"]["add_peer_aliases"])
        self.assertEqual(self.calls, [])
        self.api._cached["profiles"]["profiles"] = [{"id": "qwen-replicas", "nodes": ["halo-1"], "state": "starting"}]
        self.assertFalse(self.api.read("ops/config")["machines"][1]["add_peer_aliases"])

    def test_new_alias_is_allowed_with_unknown_or_unmarked_node_and_preserves_everything_else(self):
        for reply in (RuntimeError("unknown host key"), {"error": "install root is not marked"}):
            with self.subTest(reply=reply):
                self.config.write_text(json.dumps(self.raw), encoding="utf-8")
                self.calls.clear()
                self.replies["status"] = reply
                before = self.config.read_bytes()
                args = self.arguments()
                self.assertTrue(self.validate(args)["valid"])
                self.assertEqual(self.calls, [])
                self.assertTrue(self.save(args)["saved"])
                expected = deepcopy(self.raw)
                expected["machines"][1] = args["machine"]
                self.assertEqual(json.loads(self.config.read_text()), expected)
                self.assertEqual(self.config.with_name("cluster.json.prev").read_bytes(), before)
                self.assertEqual(self.calls, [("halo-1", "status", {"root": "/opt/hl"})])
                self.assertFalse((Path(self.dir.name) / "known_hosts").exists())

    def test_alias_replacement_removal_and_any_other_machine_change_remain_frozen(self):
        changes = [
            {"peer_ssh": {"halo-2": "replacement", "halo-3": "new-peer"}},
            {"peer_ssh": {"halo-3": "new-peer"}},
            {"peer_ssh": {}},
            {"http_address": "192.0.2.99"},
            {"ssh_target": "another-host"},
            {"ssh_port": 22},  # Even inserting a default is a separate machine edit.
            {"devices": [{"id": "gpu", "name": "changed annotation"}]},
        ]
        before = self.config.read_bytes()
        for change in changes:
            with self.subTest(change=change):
                args = self.arguments(**change)
                self.assertFalse(self.validate(args)["valid"])
                with self.assertRaisesRegex(APIError, "frozen"):
                    self.save(args)
                self.assertEqual(self.config.read_bytes(), before)

    def test_new_alias_still_obeys_config_schema(self):
        for peer, alias in (("missing-node", "new-peer"), ("halo-3", "-oProxyCommand=bad")):
            args = self.arguments(peer_ssh={"halo-2": "existing-peer", peer: alias})
            before = self.config.read_bytes()
            self.assertFalse(self.validate(args)["valid"])
            with self.assertRaisesRegex(ValueError, "invalid configuration"):
                self.save(args)
            self.assertEqual(self.config.read_bytes(), before)

    def test_active_job_and_unpublished_job_reservation_are_not_bypassed(self):
        for jobs in ({}, {"reserved": {"action": "install"}}):
            with self.subTest(jobs=jobs):
                self.api._active, self.api._jobs = "reserved", jobs
                self.assertFalse(self.api.read("ops/config")["machines"][1]["add_peer_aliases"])
                self.assertFalse(self.validate(self.arguments())["valid"])
                with self.assertRaisesRegex(APIError, "another job is running"):
                    self.save(self.arguments())
        self.assertEqual(self.calls, [])
        self.api._active, self.api._jobs = None, {}

    def test_saved_launch_live_lease_and_unknown_controller_state_are_not_bypassed(self):
        before = self.config.read_bytes()
        state = self.api.orchestrate._state_path("qwen-replicas")
        state.write_text("{}")
        with self.assertRaisesRegex(APIError, "stop profile"):
            self.save(self.arguments())
        self.assertEqual(self.calls, [])
        state.unlink()
        self.replies["status"] = {"leases": [{"lease": "l1", "state": "ready"}]}
        with self.assertRaisesRegex(APIError, "lease l1 is live"):
            self.save(self.arguments())
        self.replies["status"] = {"error": "install root is not marked"}
        with mock.patch.object(self.api.orchestrate, "status", side_effect=OSError("unreadable state")):
            with self.assertRaisesRegex(APIError, "profile status unavailable"):
                self.save(self.arguments())
        self.assertEqual(self.config.read_bytes(), before)

    def test_launch_lock_blocks_alias_additions_before_any_node_call(self):
        lock = procs.InstanceLock(self.api.orchestrate._state_path("qwen-replicas").with_suffix(".lock")).acquire()
        try:
            with self.assertRaisesRegex(APIError, "a launch is changing"):
                self.save(self.arguments())
        finally:
            lock.close()
        self.assertEqual(self.calls, [])

    def test_stale_version_refused_before_any_node_call(self):
        args = self.arguments()
        changed = self.config.read_bytes() + b"\n"
        self.config.write_bytes(changed)
        self.assertFalse(self.validate(args)["valid"])
        with self.assertRaisesRegex(APIError, "changed since it was read"):
            self.save(args)
        self.assertEqual(self.config.read_bytes(), changed)
        self.assertEqual(self.calls, [])

    def test_job_admitted_during_lease_read_prevents_commit(self):
        before = self.config.read_bytes()

        def reply(*args):
            self.api._active = "reserved-during-read"
            return {"error": "install root is not marked"}

        with mock.patch.object(ops, "_node_call", side_effect=reply):
            with self.assertRaisesRegex(APIError, "another job is running"):
                self.save(self.arguments())
        self.assertEqual(self.config.read_bytes(), before)
        self.api._active = None

    def test_config_changed_during_lease_read_is_not_overwritten(self):
        changed = self.config.read_bytes() + b"\n"

        def reply(*args):
            self.config.write_bytes(changed)
            return {"error": "install root is not marked"}

        with mock.patch.object(ops, "_node_call", side_effect=reply):
            with self.assertRaisesRegex(APIError, "changed since it was read"):
                self.save(self.arguments())
        self.assertEqual(self.config.read_bytes(), changed)


if __name__ == "__main__":
    unittest.main()
