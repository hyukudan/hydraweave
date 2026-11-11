"""Deployment contract integration with fake engine plans and HTTP replies."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hydraweave import deployment, orchestrate, render
from hydraweave.adapters import engine
from hydraweave.config import ConfigError, parse
from hydraweave.placement import plan as placement_plan
from hydraweave.placement import ds41
from hydraweave.vramplan import GIB, PlanError
from tests.test_config import example, ROOT
from tests.test_node_lease import lease
from tests.test_orchestrate import cluster_fixture


def receipt(gib=64, *, unified=True, ranks=1):
    return {"schema": "strix-deployment-v1", "model": "fake", "refused": False, "refusal": "",
            "deployment_id": "a" * 16,
            "chosen": {"topology": "single" if ranks == 1 else "tp2", "format": {"decode": "exl3"},
                       "context": 65536, "slots": 4, "ranks": ranks},
            "devices": [{"gfx_target": "gfx1151", "unified": unified, "source": "measured",
                         "mem_total_bytes": gib * GIB, "mem_free_bytes": (gib - 2) * GIB}
                        for _ in range(ranks)],
            "ranks": [{"rank": i, "node": i, "total_bytes": gib * GIB, "free_bytes": (gib - 2) * GIB,
                       "margin_bytes": GIB, "budget_bytes": (gib - 3) * GIB, "kv_bytes": 4 * GIB,
                       "required_bytes": 20 * GIB, "free_after_bytes": (gib - 23) * GIB, "fits": True,
                       "footprint": {"weights": 12 * GIB, "staging": GIB}}
                      for i in range(ranks)], "reasons": ["measured tier"]}


def capacity(total=4, busy=0, kv=GIB, queued=0):
    return {"slots_total": total, "slots_busy": busy, "kv_bytes_free": kv, "queue_depth": queued}


class PlanTests(unittest.TestCase):
    def test_measured_uma_and_discrete_plans_override_legacy_models(self):
        for gib, unified in ((64, True), (128, True), (192, True), (32, False)):
            with self.subTest(gib=gib):
                raw = receipt(gib, unified=unified)
                plan = placement_plan("ds41-node", "nonexistent-model", None, deployment=raw)
                self.assertEqual(plan.cap_bytes, (gib - 2) * GIB)
                self.assertEqual(plan.peak_bytes, 20 * GIB)
                self.assertEqual(plan.source["deployment"]["deployment_id"], "a" * 16)
                self.assertEqual(plan.notes[0], "engine chose single, context 65536, slots 4")

    def test_refused_malformed_or_nonfitting_plans_are_not_fallbacks(self):
        changes = [{"refused": True, "refusal": "KV reservation exceeds budget"},
                   {"schema": "unknown"}, {"deployment_id": "bad"}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                deployment.parse_plan(dict(receipt(), **change))
        raw = receipt()
        raw["ranks"][0]["required_bytes"] = 100 * GIB
        with self.assertRaises(PlanError):
            placement_plan("ds41-node", "unused", None, deployment=raw)

    def test_pair_uses_rank_specific_budget(self):
        raw = receipt(192, ranks=2)
        raw["ranks"][1]["free_bytes"] -= GIB
        result = placement_plan("ds41-node", None, None, deployment=raw, rank=1)
        self.assertEqual(result.cap_bytes, 189 * GIB)

    def test_status_adapter_retains_capacity_and_old_payloads(self):
        self.assertIsNone(engine.parse_status({"schema": engine.SCHEMA}).capacity)
        result = engine.parse_status({"schema": engine.SCHEMA, "capacity": capacity(), "deployment_id": "a" * 16})
        self.assertEqual(result.capacity, capacity())
        self.assertEqual(result.deployment_id, "a" * 16)

    def test_legacy_prefill_uses_cap_or_caller_measurement(self):
        from tests.test_vramplan import DS41
        large = ds41.predict_prefill("unused", vram_cap_gib=200, positions=32768, host_gib=74, sz=DS41)
        small = ds41.predict_prefill("unused", vram_cap_gib=200, positions=32768, host_gib=74,
                                     free_at_start_gib=70, sz=DS41)
        self.assertGreater(large.resident_bytes, small.resident_bytes)


class ContractTests(unittest.TestCase):
    def test_capacity_accounts_for_busy_slots_queue_and_local_reservations(self):
        contract = deployment.EngineContract()
        self.assertTrue(contract.observe({}, None, {}))
        self.assertEqual(contract.limit(8, 0), 8)
        self.assertTrue(contract.observe({"deployment_id": "a" * 16}, receipt(), {"capacity": capacity(busy=2, queued=1)}))
        self.assertEqual(contract.limit(8, 2), 3)
        contract.observe({}, receipt(), {"capacity": capacity(kv=0)})
        self.assertEqual(contract.limit(8, 2), 2)

    def test_restart_and_health_plan_mismatch_stop_admission(self):
        c = deployment.EngineContract()
        c.observe({"deployment_id": "a" * 16}, receipt(), {})
        self.assertFalse(c.observe({"deployment_id": "b" * 16}, receipt(), {}))
        self.assertIn("mismatch", c.error)
        other = dict(receipt(), deployment_id="b" * 16)
        self.assertFalse(c.observe({"deployment_id": "b" * 16}, other, {}))
        self.assertTrue(c.restart)
        self.assertFalse(c.observe({"deployment_id": "a" * 16}, receipt(), {}))

    def test_invalid_capacity_is_not_interpreted_as_free_slots(self):
        for change in ({"slots_busy": 5}, {"slots_total": True}, {"kv_bytes_free": -1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                deployment.parse_capacity(dict(capacity(), **change))

    def test_authenticated_fetch_and_legacy_endpoint_fallback(self):
        response = Mock(status=404, read=Mock(return_value=b"missing"))
        connection = Mock(getresponse=Mock(return_value=response))
        spec = SimpleNamespace(address="127.0.0.1", port=1234, api_key="test-secret")
        with patch.object(deployment.http.client, "HTTPConnection", return_value=connection):
            self.assertIsNone(deployment.fetch(spec, "/v1/deployment", 1, optional=True))
        self.assertEqual(connection.request.call_args.kwargs["headers"]["Authorization"], "Bearer test-secret")
        response.status = 401
        with patch.object(deployment.http.client, "HTTPConnection", return_value=connection), self.assertRaises(ValueError):
            deployment.fetch(spec, "/v1/deployment", 1, optional=True)


class RenderDeploymentTests(unittest.TestCase):
    def cluster(self):
        obj = example()
        for mid in ("halo-1", "halo-2"):
            obj["assets"][mid]["calibration_tokens"]["control_path"] = str(ROOT / "tests/fixtures/render/calibration.tokens")
        return obj

    def test_profile_auto_and_file_spec_render_for_both_ranks(self):
        for spec in ("auto", "/srv/deploy/gorgon tier.toml"):
            obj = self.cluster()
            obj["profiles"]["ds41-pair"]["deploy"] = {"spec": spec}
            for process in render.render_processes(parse(obj), "ds41-pair"):
                self.assertEqual(process.argv[-2:], ["--deploy", spec])

    def test_preset_deployment_removes_capacity_defaults_but_preserves_explicit_pins(self):
        cluster = parse(self.cluster())
        data = dict(render.preset("qwen-replicas"), deploy={"spec": "auto"})
        rule = data["knobs"]["--context"]
        explicit = rule.get("values", [rule["min"]])[0]
        cluster.profiles["qwen-replicas"]["overrides"] = {"--context": explicit}
        with patch.object(render, "preset", return_value=data):
            argv = render.render_processes(cluster, "qwen-replicas")[0].argv
        self.assertEqual(argv[argv.index("--context") + 1], str(explicit))
        self.assertIn("--deploy", argv)

    def test_invalid_spec_paths_and_keys(self):
        for value in ({"spec": "relative.toml"}, {"spec": "auto", "typo": True}, {"spec": "bad\npath"}):
            obj = self.cluster()
            obj["profiles"]["qwen-replicas"]["deploy"] = value
            with self.subTest(value=value), self.assertRaises(ConfigError):
                parse(obj)


class OrchestrationDeploymentTests(unittest.TestCase):
    def test_prelease_inspection_saves_plan_and_uses_inspected_argv(self):
        cluster = cluster_fixture()
        processes = [render.ProcessSpec("halo-1", "replica", ["/release/bin/engine"], {}, "qwen-stderr")]
        inspected = ["/release/bin/engine", "--deploy", "auto"]
        with patch.object(orchestrate, "_call", return_value={"supported": True, "deployment": receipt(), "argv": inspected}) as call:
            plans = orchestrate.deployment_checks(cluster, processes, {"halo-1": object()}, "qwen-replicas", {})
        self.assertEqual(plans["halo-1"]["chosen"]["slots"], 4)
        self.assertEqual(processes[0].argv, inspected)
        self.assertEqual(call.call_args.args[1], "deploy-check")
        self.assertEqual(call.call_count, 1)

    def test_legacy_release_keeps_argv_and_explicit_spec_requires_support(self):
        cluster = cluster_fixture()
        process = render.ProcessSpec("halo-1", "replica", ["/release/bin/engine"], {}, "qwen-stderr")
        with patch.object(orchestrate, "_call", return_value={"supported": False}):
            self.assertEqual(orchestrate.deployment_checks(cluster, [process], {"halo-1": object()}, "qwen-replicas", {}), {})
            self.assertEqual(process.argv, ["/release/bin/engine"])
            process.argv.extend(("--deploy", "auto"))
            with self.assertRaises(orchestrate.OrchestrationError):
                orchestrate.deployment_checks(cluster, [process], {"halo-1": object()}, "qwen-replicas", {})

    def test_topology_and_rank_plan_mismatch_refuse_before_launch(self):
        cluster = cluster_fixture(pair=True)
        processes = [render.ProcessSpec(mid, "rank" + str(i), ["/release/bin/engine"], {}, "ds41-json")
                     for i, mid in enumerate(cluster.machines)]
        clients = dict.fromkeys(cluster.machines, object())
        first, second = receipt(128, ranks=2), dict(receipt(128, ranks=2), deployment_id="b" * 16)
        with patch.object(orchestrate, "_call", side_effect=[{"supported": True, "deployment": first},
                                                            {"supported": True, "deployment": second}]):
            with self.assertRaisesRegex(orchestrate.OrchestrationError, "disagree"):
                orchestrate.deployment_checks(cluster, processes, clients, "ds41-pair", {})
        with patch.object(orchestrate, "_call", return_value={"supported": True, "deployment": receipt()}):
            with self.assertRaisesRegex(orchestrate.OrchestrationError, "topology"):
                orchestrate.deployment_checks(cluster, processes, clients, "ds41-pair", {})


class DrainDeploymentTests(unittest.TestCase):
    def child(self, ready="qwen-stderr", rank="0"):
        child = lease.Child(Path("/unused"), "fake", {"machine": "local", "ready": ready,
                            "argv": ["engine", "--rank", rank], "health_url": "http://127.0.0.1:1234/health"})
        child.process = Mock(poll=Mock(return_value=None))
        return child

    def test_all_engine_families_wait_for_drained_before_term(self):
        for ready in ("qwen-stderr", "ds41-json", "minimax-stdout"):
            child = self.child(ready)
            with patch.object(lease, "http", side_effect=[{"status": "draining", "drained": False},
                                                         {"status": "draining", "drained": True}]) as http, \
                    patch.object(lease.time, "sleep"), patch.object(lease, "group_signal") as signal:
                child.drain(float("inf"))
                self.assertEqual(http.call_count, 2)
                signal.assert_not_called()
                child.terminate()
                signal.assert_called_once()

    def test_unsupported_drain_and_follower_fallback(self):
        with patch.object(lease, "http", return_value=False) as http:
            self.child().drain(float("inf"))
            self.assertEqual(http.call_count, 1)
        with patch.object(lease, "http") as http:
            self.child("minimax-stdout", "1").drain(float("inf"))
            http.assert_not_called()

    def test_drain_is_bounded_and_health_checks_expected_identity(self):
        child = self.child()
        with patch.object(lease, "http", return_value={"status": "draining", "drained": False}) as http, \
                patch.object(lease.time, "monotonic", return_value=10):
            child.drain(5)
            self.assertEqual(http.call_count, 1)
        child.proc["deployment_id"] = "a" * 16
        with patch.object(child, "logs", return_value=("", "")), \
                patch.object(lease, "adapter_ready", return_value=True), patch.object(lease, "http", return_value=True) as http:
            self.assertTrue(child.check_ready())
            self.assertEqual(http.call_args.kwargs["deployment_id"], "a" * 16)


class NodeInspectionTests(unittest.TestCase):
    def test_node_inspects_exact_launch_argv_without_allocating_legacy_engine(self):
        module_spec = importlib.util.spec_from_file_location("deploy_under_test", ROOT / "node/hydraweave_deploy.py")
        node = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(node)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / ".hydraweave-root").write_text("synthetic-root-id")
            release = root / "releases" / ("a" * 12)
            binary = release / "bin/engine"
            binary.parent.mkdir(parents=True)
            binary.write_text("fake engine")
            (release / "MANIFEST.json").write_text('{"runtime_env": {}}')
            payload = {"root": str(root), "argv": [str(binary), "--context", "12288"],
                       "env": {}, "capacity_defaults": ["--context"]}
            help_reply = SimpleNamespace(stdout=b"--deploy-check", stderr=b"", returncode=0)
            checked = SimpleNamespace(stdout=json.dumps(receipt()).encode(), stderr=b"", returncode=0)
            with patch.dict(sys.modules, hydraweave_lease=lease), patch.object(node.subprocess, "run", side_effect=[help_reply, checked]) as run:
                reply = node.run("deploy-check", payload)
            self.assertNotIn("error", reply)
            self.assertEqual(reply["argv"], [str(binary), "--deploy", "auto"])
            self.assertEqual(run.call_args.args[0], reply["argv"] + ["--deploy-check"])
            self.assertEqual(run.call_count, 2)  # help, then one allocation-free inspection
            help_reply.stdout = b"old engine flags"
            with patch.dict(sys.modules, hydraweave_lease=lease), patch.object(node.subprocess, "run", return_value=help_reply) as run:
                reply = node.run("deploy-check", payload)
            self.assertFalse(reply["supported"])
            self.assertEqual(run.call_count, 1)


class RouterBridgeTests(unittest.TestCase):
    def test_real_pool_uses_fake_engine_capacity_and_detects_restart(self):
        source = ROOT / "third_party/tensormeld/src"
        sys.path.insert(0, str(source))
        self.addCleanup(lambda: sys.path.remove(str(source)))
        from tensormeld import replica_router as router
        with patch.object(router, "ReplicaPool", router.ReplicaPool), patch.object(router, "HealthMonitor", router.HealthMonitor):
            deployment.install_router_bridge(router)
            config = router.RouterConfig.parse({"schema": "tensormeld/replica-router-v1",
                "listen": {"address": "127.0.0.1", "port": 1234}, "replicas": [
                    {"id": name, "node": name, "address": "127.0.0.1", "port": port, "max_inflight": 8}
                    for name, port in (("full", 1235), ("free", 1236))], "limits": {"queue_timeout_s": 0}})
            pool = router.ReplicaPool(config)
            monitor = router.HealthMonitor(pool)
            identity = "a" * 16

            def fake(spec, path, timeout, **kwargs):
                if path == "/health":
                    return {"deployment_id": identity}
                if path == "/v1/deployment":
                    return dict(receipt(), deployment_id=identity)
                return {"capacity": capacity(busy=4 if spec.id == "full" else 2)}

            with patch.object(deployment, "fetch", side_effect=fake):
                monitor.check_once()
                self.assertEqual(pool.acquire().id, "free")
                self.assertEqual(pool.acquire().id, "free")
                with self.assertRaises(router.NoReplicaAvailable):
                    pool.acquire()
                identity = "b" * 16
                monitor.check_once()
                with self.assertRaises(router.NoReplicaAvailable):
                    pool.acquire()
                snapshot = pool.snapshot()["replicas"]
                self.assertTrue(all(row["deployment_restarted"] for row in snapshot))

    def test_old_engine_endpoints_preserve_configured_routing(self):
        c = deployment.EngineContract()
        self.assertTrue(c.observe({}, None, None))
        self.assertEqual(c.limit(8, 3), 8)
