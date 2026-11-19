"""Pinned browser downloads: actual portal jobs, config transactions and node receipts."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from hydraweave import admission, assets, downloads, library_jobs
from hydraweave.config import load
from hydraweave.telemetry.store import Store
from hydraweave.web import features
from hydraweave.web.api import API, APIError
from hydraweave.web.features import assets as feature
from tests.test_config import example


class AssetFeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        raw = example()
        for machine in raw["machines"]:
            if machine["id"] == "halo-1":
                machine["model_roots"] = ["/opt/assets"]
        self.path = self.base / "cluster.json"
        self.path.write_text(json.dumps(raw))
        self.api = API(self.path, audit_path=self.base / "audit.jsonl")
        self.api.admission_db = admission.Admission(":memory:")
        self.addCleanup(self.api.admission_db.close)
        self.api.telemetry = Store(":memory:")
        self.addCleanup(self.api.telemetry.close)
        registry = features.Registry()
        feature.register(registry)
        features.reset(registry)
        self.addCleanup(features.reset)
        self.addCleanup(self.api.close_features)
        self.pin = {"file": "model.gguf", "bytes": 7, "sha256": hashlib.sha256(b"payload").hexdigest()}
        self.source = {"status": "download", "repo": "owner/model", "revision": "a" * 40, "files": [self.pin]}
        p = patch.object(downloads, "sources", return_value={"model": self.source})
        p.start(); self.addCleanup(p.stop)
        p = patch.object(assets, "manifest_path", return_value=self.base / "manifest.json")
        p.start(); self.addCleanup(p.stop)
        self.arguments = {"profile": "qwen-replicas", "machine": "halo-1", "asset": "model", "path": "/opt/assets/model.gguf"}
        self.calls = []
        self.entry = {"bytes": 7, "sha256": self.pin["sha256"], "mtime_ns": 123, "inode": 7, "dev": 1}
        self.remote_state = {"job": "d" * 32, "state": "succeeded", "verified": {self.arguments["path"]: self.entry},
                             "bytes_transferred": 7, "bytes_reused": 0}
        p = patch.object(downloads, "_node", side_effect=self.node)
        p.start(); self.addCleanup(p.stop)
        p = patch.object(downloads.orchestrate, "_lab", return_value="/opt/lab")
        p.start(); self.addCleanup(p.stop)

    def node(self, machine, verb, payload, **kwargs):
        self.calls.append((verb, payload))
        if verb == "download-prepare":
            return {"prepared": True, "directory": payload["directory"]}
        if verb == "download-status":
            return self.remote_state
        if verb == "download-cancel":
            return {"state": "cancelled"}
        if verb == "stat-assets":
            return {"assets": {self.arguments["path"]: self.entry}}
        raise AssertionError(verb)

    def start(self, confirm=None):
        frozen = feature._selection(self.api, self.arguments)[1]
        confirm = confirm or library_jobs.review_confirm("a" * 12, frozen, 12)
        with patch.object(downloads, "start", return_value={"job": "d" * 32}) as started, \
                patch.object(downloads, "plan", return_value={"confirm": "a" * 12}):
            accepted = self.api.mutate("assets/download", {**self.arguments, "confirm": confirm})
            for _ in range(200):
                row = self.api.read("jobs/" + accepted["id"])
                if row["state"] != "running":
                    self.started_calls = started.call_count
                    return row
                time.sleep(.01)
        self.fail("job did not finish")

    def test_read_is_passive_prepare_is_explicit_and_plan_has_real_pin(self):
        result = self.api.read("assets", {})
        self.assertTrue(any(r.get("downloadable") for r in result["assets"]))
        self.assertEqual(self.calls, [])
        self.api.feature_post("assets/prepare", self.arguments)
        self.assertEqual(self.calls[0][0], "download-prepare")
        with patch.object(downloads, "plan", return_value={"confirm": "a" * 12, "files": [], "disk": []}) as planned:
            _, result = self.api.feature_post("assets/plan", self.arguments)
        proposed = planned.call_args.args[0]
        self.assertEqual(proposed.assets["halo-1"]["model"], {k: v for k, v in {**self.pin, "path": self.arguments["path"]}.items() if k != "file"})
        self.assertEqual(result["confirm"], library_jobs.review_confirm("a" * 12, result["configuration"], 12))
        self.assertEqual(len(result["configuration"]), 64)

    def test_verified_receipt_registers_config_and_integrity_cache(self):
        row = self.start()
        self.assertEqual(row["state"], "succeeded", row)
        self.assertTrue(row["result"]["registered"])
        self.assertEqual(load(self.path).assets["halo-1"]["model"]["sha256"], self.pin["sha256"])
        self.assertEqual(assets.read_manifest()["halo-1"][self.arguments["path"]]["inode"], 7)
        self.assertTrue(any(verb == "download-status" and args["renew"] for verb, args in self.calls))

    def test_disjoint_download_jobs_both_register_without_losing_each_others_config(self):
        arguments = [self.arguments, {**self.arguments, "machine": "halo-2"}]
        ready = threading.Barrier(2)
        entries = {"halo-1": self.entry, "halo-2": {**self.entry, "inode": 8}}

        def node(machine, verb, payload, **kwargs):
            if verb == "download-status":
                return {**self.remote_state, "job": payload["job"],
                        "verified": {self.arguments["path"]: entries[machine.id]}}
            if verb == "stat-assets":
                # Both portal jobs have downloaded and checked their receipts
                # before either enters the shared configuration transaction.
                ready.wait(timeout=5)
                return {"assets": {self.arguments["path"]: entries[machine.id]}}
            return self.node(machine, verb, payload, **kwargs)

        with patch.object(downloads, "_node", side_effect=node), \
                patch.object(downloads, "plan", return_value={"confirm": "a" * 12}), \
                patch.object(downloads, "start", side_effect=lambda cluster, profile, asset, mid, confirm, **kw:
                             {"job": ("d" if mid == "halo-1" else "e") * 32}) as started:
            plans = [self.api.feature_post("assets/plan", args)[1] for args in arguments]
            accepted = [self.api.mutate("assets/download", {**args, "confirm": plan["confirm"]})
                        for args, plan in zip(arguments, plans)]
            until = time.monotonic() + 8
            while time.monotonic() < until:
                rows = [self.api.read("jobs/" + job["id"]) for job in accepted]
                if all(row["state"] != "running" for row in rows):
                    break
                time.sleep(.01)
            self.assertEqual([row["state"] for row in rows], ["succeeded", "succeeded"], rows)
            self.assertEqual(started.call_count, 2)
        current = load(self.path)
        manifest = assets.read_manifest()
        for args in arguments:
            mid = args["machine"]
            self.assertEqual(current.assets[mid]["model"],
                             {"path": args["path"], "bytes": self.pin["bytes"], "sha256": self.pin["sha256"]})
            self.assertEqual(manifest[mid][args["path"]]["inode"], entries[mid]["inode"])

    def test_unrelated_edits_after_review_and_during_registration_are_preserved(self):
        frozen = feature._selection(self.api, self.arguments)[1]
        confirm = library_jobs.review_confirm("a" * 12, frozen, 12)
        raw = json.loads(self.path.read_text())
        raw["machines"][2]["http_address"] = "192.0.2.99"
        raw["profiles"]["ds41-pair"]["port"] = 18432
        self.path.write_text(json.dumps(raw))

        def node(machine, verb, payload, **kwargs):
            result = self.node(machine, verb, payload, **kwargs)
            if verb == "stat-assets":
                current = json.loads(self.path.read_text())
                current["router"]["max_inflight"] = 9
                current["assets"]["halo-1"]["sidecar"]["bytes"] += 1
                self.path.write_text(json.dumps(current))
            return result

        with patch.object(downloads, "_node", side_effect=node):
            row = self.start(confirm)
        self.assertEqual(row["state"], "succeeded", row)
        current = load(self.path)
        self.assertEqual(current.machines["halo-2"].http_address, "192.0.2.99")
        self.assertEqual(current.profiles["ds41-pair"]["port"], 18432)
        self.assertEqual(current.router["max_inflight"], 9)
        self.assertEqual(current.assets["halo-1"]["sidecar"]["bytes"], raw["assets"]["halo-1"]["sidecar"]["bytes"] + 1)

    def test_bad_receipt_never_changes_configuration(self):
        before = self.path.read_bytes()
        self.remote_state["verified"] = {}
        row = self.start()
        self.assertEqual(row["state"], "failed", row)
        self.assertIn("verified final", row["error"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.base / "manifest.json").exists())

    def test_path_escape_extra_url_and_changed_queued_config_are_refused(self):
        for changed in ({"path": "/etc/model.gguf"}, {"url": "https://example.org/model"}, {"path": "/opt/assets/../model.gguf"}):
            with self.subTest(changed), self.assertRaises((APIError, ValueError)):
                self.api.feature_post("assets/plan", {**self.arguments, **changed})
        validated = feature.validate(self.api, {**self.arguments, "confirm": "a" * 12})
        raw = json.loads(self.path.read_text())
        raw["assets"]["halo-1"]["model"]["bytes"] = 99
        self.path.write_text(json.dumps(raw))
        from hydraweave.web.api import JobContext
        with patch.object(downloads, "start", side_effect=AssertionError("started")), self.assertRaisesRegex(ValueError, "changed"):
            feature.run(self.api, validated, JobContext(60))

    def test_overlapping_job_holds_the_machine_until_cleanup(self):
        hold = self.api.admission_db.acquire("bench", {"halo-1"}, label="a running model job")
        self.addCleanup(lambda: self.api.admission_db.release(hold))
        with self.assertRaises(APIError) as refused:
            self.api.mutate("assets/download", {**self.arguments, "confirm": "a" * 12})
        self.assertEqual(refused.exception.status, 409)
        self.assertEqual(self.calls, [])

    def test_transport_changed_after_review_cannot_retarget_confirmation(self):
        frozen = feature._selection(self.api, self.arguments)[1]
        confirm = library_jobs.review_confirm("a" * 12, frozen, 12)
        raw = json.loads(self.path.read_text())
        raw["machines"][1]["http_address"] = "192.0.2.99"
        self.path.write_text(json.dumps(raw))
        row = self.start(confirm)
        self.assertEqual(row["state"], "failed", row)
        self.assertIn("configuration changed", row["error"])
        self.assertEqual(self.started_calls, 0)

    def test_selected_profile_or_source_changed_after_review_requires_new_confirmation(self):
        for change in ("profile", "source"):
            with self.subTest(change=change):
                frozen = feature._selection(self.api, self.arguments)[1]
                confirm = library_jobs.review_confirm("a" * 12, frozen, 12)
                if change == "profile":
                    raw = json.loads(self.path.read_text())
                    raw["profiles"]["qwen-replicas"]["port"] = 18502
                    self.path.write_text(json.dumps(raw))
                else:
                    self.source["revision"] = "b" * 40
                row = self.start(confirm)
                self.assertEqual(row["state"], "failed", row)
                self.assertIn("configuration changed", row["error"])
                self.assertEqual(self.started_calls, 0)

    def test_target_changed_during_download_is_not_registered(self):
        before = load(self.path).assets["halo-1"]["model"]

        def node(machine, verb, payload, **kwargs):
            result = self.node(machine, verb, payload, **kwargs)
            if verb == "stat-assets":
                raw = json.loads(self.path.read_text())
                raw["machines"][1]["install_root"] = "/opt/changed-hydraweave"
                self.path.write_text(json.dumps(raw))
            return result

        with patch.object(downloads, "_node", side_effect=node):
            row = self.start()
        self.assertEqual(row["state"], "failed", row)
        self.assertIn("configuration changed", row["error"])
        self.assertEqual(load(self.path).assets["halo-1"]["model"], before)
        self.assertFalse((self.base / "manifest.json").exists())

    def test_cancellation_during_final_stat_keeps_configuration_and_cache_untouched(self):
        from hydraweave.web.api import JobContext
        frozen = feature._selection(self.api, self.arguments)[1]
        validated = feature.validate(self.api, {**self.arguments, "confirm": library_jobs.review_confirm("a" * 12, frozen, 12)})
        before = self.path.read_bytes()
        ctx = JobContext(60)
        def node(machine, verb, payload, **kw):
            value = self.node(machine, verb, payload, **kw)
            if verb == "stat-assets":
                ctx.cancel("operator cancelled while stat was in flight")
            return value
        with patch.object(downloads, "_node", side_effect=node), \
                patch.object(downloads, "plan", return_value={"confirm": "a" * 12}), \
                patch.object(downloads, "start", return_value={"job": "d" * 32}):
            self.assertIsNone(feature.run(self.api, validated, ctx))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.base / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
