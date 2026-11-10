"""Engine settings use pinned knobs, preserve the production render and save only reviewed idle config."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from hydraweave import admission, config, engine_settings as settings, procs, render
from hydraweave.web import features
from hydraweave.web.api import API, APIError
from hydraweave.web.features import engine_settings as feature, ops
from tests.test_engines import cluster_obj


class EngineSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "cluster.json"
        self.raw = cluster_obj()
        self.write(self.raw)
        self.orchestrator = SimpleNamespace(
            status=mock.Mock(return_value={}),
            _state_path=lambda p: self.root / ("up-" + p + ".json"),
            _absent=mock.Mock(return_value=False), _client=mock.Mock(),
        )
        self.api = API(self.path, orchestrator=self.orchestrator,
                       audit_path=self.root / "audit.jsonl")
        self.api.admission_db = admission.Admission(":memory:")
        self.addCleanup(self.api.admission_db.close)
        self.api._cached = {"profiles": {"profiles": []}}
        self.registry = features.load(("engine_settings",))
        patcher = mock.patch.object(features, "registry", return_value=self.registry)
        patcher.start()
        self.addCleanup(patcher.stop)
        node = mock.patch.object(ops, "_node_call", return_value={"leases": []})
        self.node = node.start()
        self.addCleanup(node.stop)

    def write(self, raw):
        self.path.write_text(json.dumps(raw), encoding="utf-8")

    def version(self):
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def post(self, endpoint, arguments):
        return self.api.feature_post("engine-settings/" + endpoint, arguments)[1]

    def edit(self, changes, profile="ds41-pair", **kw):
        return {"version": self.version(), "profile": profile, "changes": changes, **kw}

    def test_get_reads_only_config_and_cached_state(self):
        self.api._cached = None
        with mock.patch.object(self.api, "snapshot", side_effect=AssertionError("GET must not poll")), \
                mock.patch.object(ops, "_node_call", side_effect=AssertionError("GET must not call nodes")), \
                mock.patch.object(ops, "write_config", side_effect=AssertionError("GET must not write")):
            doc = self.api.read("engine-settings")
        self.assertEqual(doc["version"], self.version())
        self.assertEqual(len(doc["presets"]), len(config.PRESET_NAMES))
        self.assertFalse(doc["runtime_applied"])
        self.assertFalse((self.root / "audit.jsonl").exists())
        qwen = next(p for p in doc["profiles"] if p["id"] == "qwen-replicas")
        self.assertIsNone(qwen["capabilities"]["runtime"])
        context = next(c for c in qwen["controls"] if c["key"] == "--context")
        self.assertEqual(context["choices"], [{"value": 12288, "label": "12288"}])
        self.assertFalse(context["editable"])
        self.assertIn("another qualified release", context["why"])

    def test_preview_is_the_existing_renderer_and_no_edits_preserve_all_fields(self):
        new, cluster = settings.apply_changes(self.raw, "qwen-replicas", {})
        self.assertEqual(new, self.raw)
        actual = settings.preview(cluster, "qwen-replicas")
        self.assertTrue(actual["rendered"])
        self.assertEqual(actual["processes"][0]["argv"], render.render_processes(cluster, "qwen-replicas")[0].argv)
        self.assertEqual(actual["router"], render.render_router(cluster, "qwen-replicas"))

    def test_managed_launch_resources_are_deferred_not_manual_setup_errors(self):
        raw = deepcopy(self.raw)
        p = raw["profiles"]["ds41-pair"]
        p.pop("release_id", None)
        p.pop("api_key_files", None)
        raw["router"].pop("replica_api_key_files", None)
        raw["router"].pop("api_key_file", None)
        actual = settings.preview(config.parse(raw), "ds41-pair")
        self.assertFalse(actual["rendered"])
        self.assertIsNone(actual["error"])
        self.assertFalse(actual["pending_setup"])
        generated = {row["resource"] for row in actual["generated_at_launch"]}
        self.assertTrue({"release_id", "api_key_files", "replica_api_key_files", "api_key_file"} <= generated)
        self.assertTrue(actual["deferred"])
        raw["assets"] = {}
        actual = settings.preview(config.parse(raw), "ds41-pair")
        self.assertIn("missing asset records", actual["error"])
        self.assertTrue(all(row["part"] == "assets" for row in actual["pending_setup"]))
        raw["profiles"]["minimax-pair"].pop("release_id", None)
        actual = settings.preview(config.parse(raw), "minimax-pair")
        self.assertIn("engines bind loopback only", actual["error"])

    def test_advanced_settings_have_real_effect_and_auto_removes_override(self):
        args = self.edit({"advanced": {"serve_sched": "off", "dspark_max_inflight": 0}})
        before = self.path.read_bytes()
        checked = self.post("validate", args)
        self.assertTrue(checked["valid"])
        self.assertFalse(checked["qualification"]["production"])
        for p in checked["preview"]["processes"]:
            self.assertEqual(p["env"]["STRIX_DS41_SERVE_SCHED"], "off")
            self.assertIn("--dspark-max-inflight", p["argv"])
        self.assertEqual(before, self.path.read_bytes())
        saved = self.post("save", args)
        self.assertTrue(saved["saved"] and saved["restart_required"])
        self.assertEqual((self.root / "cluster.json.prev").read_bytes(), before)
        self.assertEqual(config.load(self.path).profiles["ds41-pair"]["advanced"]["serve_sched"], "off")
        restored = self.post("save", self.edit({"advanced": {"serve_sched": None, "dspark_max_inflight": None}}))
        self.assertNotIn("advanced", restored["configuration"])
        self.assertTrue(restored["qualification"]["production"])
        self.assertNotIn("STRIX_DS41_SERVE_SCHED", restored["preview"]["processes"][0]["env"])

    def test_port_edits_clear_legacy_override_and_update_router(self):
        self.raw["profiles"]["ds41-pair"]["overrides"] = {"--http-port": 19000}
        self.write(self.raw)
        saved = self.post("save", self.edit({"port": 19001}))
        self.assertNotIn("overrides", saved["configuration"])
        self.assertEqual(saved["configuration"]["port"], 19001)
        self.assertEqual(saved["preview"]["router"]["replicas"][0]["port"], 19001)
        reset = self.post("save", self.edit({"port": None}))
        self.assertNotIn("port", reset["configuration"])
        self.assertEqual(reset["preview"]["router"]["replicas"][0]["port"], config.preset("ds41-pair")["port"])

    def test_reject_unqualified_context_flags_shell_and_wrong_types(self):
        bad = ({"overrides": {"--context": 32768}}, {"overrides": {"--http-bind": "0.0.0.0"}},
               {"advanced": {"serve_sched": "off; touch /tmp/x"}}, {"advanced": {"dspark_ranks": True}},
               {"advanced": {"dspark_ranks": 3}}, {"env": {"LD_PRELOAD": "/tmp/x"}},
               {"port": True}, {"port": 65536}, {"port": 20000, "overrides": {"--http-port": 20001}})
        before = self.path.read_bytes()
        for changes in bad:
            with self.subTest(changes=changes):
                checked = self.post("validate", self.edit(changes))
                self.assertFalse(checked["valid"], checked)
                with self.assertRaises(APIError):
                    self.post("save", self.edit(changes))
                self.assertEqual(self.path.read_bytes(), before)

    def test_stale_version_refused_and_write_during_checks_is_not_lost(self):
        args = self.edit({"port": 19002})
        other = deepcopy(self.raw)
        other["profiles"]["qwen-replicas"]["port"] = 19001
        self.write(other)
        self.assertTrue(self.post("validate", args)["stale"])
        with self.assertRaises(APIError) as caught:
            self.post("save", args)
        self.assertEqual(caught.exception.status, 409)
        args = self.edit({"port": 19002})
        newer = deepcopy(other)
        newer["router"]["max_inflight"] = 2
        with mock.patch.object(ops, "occupancy", side_effect=lambda *a: (self.write(newer) or None, False)):
            with self.assertRaises(APIError):
                self.post("save", args)
        self.assertEqual(json.loads(self.path.read_text()), newer)

    def test_active_launch_unknown_node_and_saved_state_are_refused(self):
        before = self.path.read_bytes()
        for reason, unknown in (("lease abc is live", False), ("node status unavailable", True)):
            with mock.patch.object(ops, "occupancy", return_value=(reason, unknown)):
                with self.assertRaises(APIError) as caught:
                    self.post("save", self.edit({"port": 19002}))
                self.assertEqual(caught.exception.status, 409)
                self.assertEqual(before, self.path.read_bytes())
        state = self.orchestrator._state_path("ds41-pair")
        state.write_text("{}")
        with self.assertRaises(APIError) as caught:
            self.post("save", self.edit({"port": 19002}))
        self.assertIn("saved running state", str(caught.exception))

    def test_missing_tool_is_accepted_only_when_absence_is_proven(self):
        reason = "the node's lease records are unreadable (missing tool)"
        with mock.patch.object(ops, "occupancy", return_value=(reason, True)):
            with self.assertRaises(APIError):
                self.post("save", self.edit({"port": 19002}))
            self.orchestrator._absent.return_value = True
            self.assertTrue(self.post("save", self.edit({"port": 19002}))["saved"])

    def test_block_cached_live_profile_and_mutating_job(self):
        self.api._cached = {"profiles": {"profiles": [{"id": "other", "state": "running", "nodes": ["halo-1"]}]}}
        doc = self.api.read("engine-settings", {"profile": "ds41-pair"})
        self.assertFalse(doc["profiles"][0]["editable"])
        self.api._active = "busy"
        self.api._jobs["busy"] = {"action": "up", "state": "running"}
        with self.assertRaises(APIError):
            self.post("save", self.edit({"port": 19002}))

    def test_mutating_job_that_starts_during_checks_is_refused(self):
        def start(*args):
            self.api._active = "busy"
            self.api._jobs["busy"] = {"action": "install", "state": "running"}
            return None, False
        before = self.path.read_bytes()
        with mock.patch.object(ops, "occupancy", side_effect=start):
            with self.assertRaises(APIError):
                self.post("save", self.edit({"port": 19002}))
        self.assertEqual(before, self.path.read_bytes())

    def test_profile_launch_lock_and_bench_admission_block_edits(self):
        held = procs.InstanceLock(self.orchestrator._state_path("ds41-pair").with_suffix(".lock")).acquire()
        try:
            with self.assertRaises(APIError) as caught:
                self.post("save", self.edit({"port": 19002}))
            self.assertIn("started or stopped", str(caught.exception))
        finally:
            held.close()
        hold = self.api._admission().acquire("bench", {"halo-1"}, label="comparison")
        try:
            with self.assertRaises(APIError) as caught:
                self.post("save", self.edit({"port": 19002}))
            self.assertIn("benchmark", str(caught.exception))
        finally:
            self.api._admission().release(hold)

    def test_partial_asset_update_preserves_other_roles_and_checks_shared_nodes(self):
        asset = {"path": "/models/new/model.gguf", "bytes": 1234, "sha256": "b" * 64}
        before = deepcopy(self.raw)
        with mock.patch.object(ops, "occupancy", return_value=(None, False)) as check:
            saved = self.post("save", self.edit({"assets": {"halo-1": {"ds41_model": asset}}}))
        after = config.read_json(self.path)
        self.assertEqual(after["assets"]["halo-1"]["ds41_model"], asset)
        self.assertEqual(after["assets"]["halo-1"]["model"], before["assets"]["halo-1"]["model"])
        self.assertTrue(any(c.args[2] == "halo-1" for c in check.call_args_list))
        self.assertTrue(any(r["path"] == "assets.halo-1.ds41_model" for r in saved["changes"]))
        self.assertEqual(next(a for a in saved["assets"] if a["machine"] == "halo-1" and a["key"] == "ds41_model")["verification"], "declared")
        for changes in ({"assets": {"halo-1": {"ds41_model": dict(asset, sha256="unknown")}}},
                        {"assets": {"workstation": {"ds41_model": asset}}},
                        {"assets": {"halo-1": {"arbitrary": asset}}}):
            self.assertFalse(self.post("validate", self.edit(changes))["valid"])

    def test_create_before_install_is_inert_and_does_not_invent_release_or_assets(self):
        self.raw["profiles"] = {}
        self.raw["assets"] = {}
        self.write(self.raw)
        args = {"version": self.version(), "id": "chat", "preset": "qwen-replicas", "nodes": ["halo-1"]}
        checked = self.post("validate", dict(args, op="create"))
        self.assertTrue(checked["valid"])
        self.assertFalse(checked["preview"]["rendered"])
        with mock.patch.object(ops, "occupancy", side_effect=AssertionError("inert profile creation needs no node read")):
            created = self.post("create", args)
        self.assertTrue(created["saved"])
        p = config.load(self.path).profiles["chat"]
        self.assertEqual(p, {"preset": "qwen-replicas", "nodes": ["halo-1"]})
        self.assertTrue(all(a["value"] is None for a in created["assets"]))
        self.node.assert_not_called()
        with self.assertRaises(APIError):
            self.post("create", dict(args, version=self.version()))

    def test_create_pair_members_and_ssh_worker_contract(self):
        args = {"version": self.version(), "id": "pair", "preset": "ds41-pair", "rank0": "halo-1", "rank1": "halo-2"}
        self.assertTrue(self.post("create", args)["saved"])
        for bad in (dict(args, id="bad", rank1="halo-1"),
                    dict(args, id="bad", rank1="workstation"),
                    {"id": "bad", "preset": "qwen-replicas", "nodes": ["workstation"]},
                    dict(args, id="bad", nodes=["halo-1"]),
                    dict(args, id="bad", release_id="0" * 12)):
            with self.subTest(bad=bad), self.assertRaises(APIError):
                self.post("create", dict(bad, version=self.version()))
        self.raw["machines"][0]["os"] = "linux"
        self.raw["machines"][0]["install_root"] = "/opt/controller"
        self.raw["profiles"] = {}
        self.write(self.raw)
        with self.assertRaisesRegex(APIError, "SSH"):
            self.post("create", {"version": self.version(), "id": "local-chat", "preset": "qwen-replicas", "nodes": ["workstation"]})

    def test_two_reviewed_saves_cannot_overwrite_each_other(self):
        args = self.edit({"port": 19002})
        results = []
        barrier = threading.Barrier(2)
        def save(port):
            barrier.wait()
            try:
                results.append(self.post("save", dict(args, changes={"port": port})))
            except APIError as exc:
                results.append(exc)
        workers = [threading.Thread(target=save, args=(p,)) for p in (19002, 19003)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(10)
            self.assertFalse(worker.is_alive())
        self.assertEqual(sum(isinstance(r, APIError) and r.status == 409 for r in results), 1)
        self.assertEqual(sum(isinstance(r, dict) and r.get("saved") for r in results), 1)

    def test_registry_and_query_validation(self):
        self.assertFalse(self.registry.failed)
        for query in ({"profile": "missing"}, {"profile": []}, {"profile": 12}, {"unexpected": "value"}):
            with self.assertRaises(APIError):
                self.api.read("engine-settings", query)
        with self.assertRaises(APIError):
            self.post("save", {"profile": "ds41-pair", "changes": {"port": 19002}})


if __name__ == "__main__":
    unittest.main()
