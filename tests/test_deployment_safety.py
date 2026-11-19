"""Current engine receipts, reservation races and bounded node inspection."""
from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

from hydraweave import deployment, orchestrate, render
from hydraweave.placement import plan
from hydraweave.vramplan import GIB, PlanError
from tests.test_deployment import capacity, receipt
from tests.test_config import ROOT
from tests.test_node_lease import lease
from tests.test_orchestrate import cluster_fixture


class DrainCleanupTests(unittest.TestCase):
    def test_unexpected_drain_failure_still_stops_all_owned_children(self):
        children = [Mock(), Mock()]
        for child in children:
            child.process.poll.return_value = 0
        children[0].drain.side_effect = RuntimeError("broken drain response")
        with patch.object(lease, "pair_leader", return_value=False), patch.object(lease, "group_signal"), \
                self.assertRaisesRegex(RuntimeError, "after owned-process cleanup"):
            lease.stop_children(children)
        for child in children:
            child.terminate.assert_called_once()
            child.process.wait.assert_called_once()
            child.close.assert_called_once()


class CurrentReceiptTests(unittest.TestCase):
    def test_margin_cannot_be_spent_as_weights(self):
        document = receipt()
        rank = document["ranks"][0]
        rank.update(budget_bytes=62 * GIB, required_bytes=62 * GIB, free_after_bytes=0)
        with self.assertRaisesRegex(PlanError, "budget"):
            plan("ds41-node", None, None, deployment=document)

    def test_rank_cannot_claim_another_devices_memory_or_same_device_twice(self):
        for key, value in (("free_bytes", 63 * GIB), ("total_bytes", 128 * GIB)):
            document = receipt()
            document["ranks"][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                deployment.parse_plan(document)
        document = receipt(ranks=2)
        document["ranks"][1]["node"] = 0
        with self.assertRaises(ValueError):
            deployment.parse_plan(document)

    def test_complete_native_footprint_must_account_for_all_owners(self):
        document = receipt()
        document["ranks"][0]["footprint"].update(workspace=2 * GIB, extra=[{"name": "cache", "bytes": GIB}])
        self.assertEqual(deployment.parse_plan(document), document)
        document["ranks"][0]["footprint"]["extra"][0]["bytes"] += 1
        with self.assertRaisesRegex(ValueError, "owners"):
            deployment.parse_plan(document)

    def test_fixed_slot_engines_have_nullable_shared_kv_capacity(self):
        contract = deployment.EngineContract()
        self.assertTrue(contract.observe({}, None, {"capacity": capacity(kv=None)}))
        self.assertEqual(contract.limit(8, 0), 4)
        contract.observe({}, None, {"capacity": capacity(kv=0)})
        self.assertEqual(contract.limit(8, 0), 0)

    def test_bool_is_not_a_requested_rank(self):
        with self.assertRaises(PlanError):
            plan("ds41-node", None, None, deployment=receipt(ranks=2), rank=True)

    def test_inspection_hash_change_does_not_imply_different_pair_choice(self):
        cluster = cluster_fixture(pair=True)
        processes = [render.ProcessSpec(mid, "rank" + str(i),
                                        ["/release/bin/engine", "--deploy", "/srv/deploy.toml"], {}, "ds41-json")
                     for i, mid in enumerate(cluster.machines)]
        barrier = threading.Barrier(2)

        def inspect(client, verb, payload, **kwargs):
            barrier.wait(3)  # Sequential prechecks cannot complete this fixture.
            document = receipt(128, ranks=2)
            document["deployment_id"] = client * 16
            return {"supported": True, "deployment": document, "argv": payload["argv"]}

        clients = dict(zip(cluster.machines, ("a", "b")))
        with patch.object(orchestrate, "_call", side_effect=inspect):
            result = orchestrate.deployment_checks(cluster, processes, clients, "ds41-pair", {})
        self.assertEqual(len(result), 2)
        self.assertEqual({row["deployment_id"] for row in result.values()}, {"a" * 16, "b" * 16})

    def test_pair_auto_collects_both_real_devices_before_inspecting(self):
        cluster = cluster_fixture(pair=True)
        processes = [render.ProcessSpec(mid, "rank" + str(i),
                                        ["/release/bin/engine", "--deploy", "auto"], {}, "ds41-json")
                     for i, mid in enumerate(cluster.machines)]
        seen = []

        def inspect(client, verb, payload, **kwargs):
            seen.append(payload)
            if payload.get("inventory_only"):
                document = receipt()
                document.update(refused=True, refusal="missing measured peer")
                return {"supported": True, "deployment": document}
            self.assertEqual(len(payload["inventory"]), 2)
            argv = list(payload["argv"])
            argv[-1] = payload["root"] + "/run/deployment-" + "a" * 64 + ".toml"
            return {"supported": True, "deployment": receipt(ranks=2), "argv": argv}

        with patch.object(orchestrate, "_call", side_effect=inspect):
            result = orchestrate.deployment_checks(cluster, processes, dict.fromkeys(cluster.machines), "ds41-pair", {})
        self.assertEqual(len(result), 2)
        self.assertTrue(all(row.get("inventory_only") for row in seen[:2]))
        self.assertTrue(all("inventory" in row for row in seen[2:]))


class ReservationTests(unittest.TestCase):
    def pool(self, identities=("one",)):
        source = ROOT / "third_party/tensormeld/src"
        sys.path.insert(0, str(source))
        self.addCleanup(lambda: sys.path.remove(str(source)))
        from tensormeld import replica_router as router
        for name in ("ReplicaPool", "HealthMonitor"):
            original = getattr(router, name)
            self.addCleanup(setattr, router, name, original)
        deployment.install_router_bridge(router)
        config = router.RouterConfig.parse({"schema": "tensormeld/replica-router-v1",
            "listen": {"address": "127.0.0.1", "port": 1234}, "replicas": [
                {"id": name, "node": name, "address": "127.0.0.1", "port": 1235 + i, "max_inflight": 8}
                for i, name in enumerate(identities)],
            "limits": {"queue_timeout_s": 0}})
        pool = router.ReplicaPool(config)
        return router, pool, router.HealthMonitor(pool)

    def test_probe_does_not_return_locally_reserved_slots_to_capacity(self):
        router, pool, monitor = self.pool()

        def fetch(spec, path, timeout, **kwargs):
            return {} if path == "/health" else None if path == "/v1/deployment" else {"capacity": capacity(total=2)}

        with patch.object(deployment, "fetch", side_effect=fetch):
            monitor.check_once()
        entered, release = threading.Event(), threading.Event()

        def blocked(spec, path, timeout, **kwargs):
            if path == "/api/status":
                entered.set()
                if not release.wait(3):
                    raise ValueError("test probe timed out")
            return fetch(spec, path, timeout, **kwargs)

        with patch.object(deployment, "fetch", side_effect=blocked):
            thread = threading.Thread(target=monitor.check_once)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(pool.acquire().id, "one")
            finally:
                release.set()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(pool.acquire().id, "one")
        with self.assertRaises(router.NoReplicaAvailable):
            pool.acquire()

    def test_immutable_live_plan_is_only_transferred_again_when_identity_changes(self):
        _, pool, monitor = self.pool()
        identity, calls = "a" * 16, []

        def fetch(spec, path, timeout, **kwargs):
            calls.append(path)
            if path == "/health":
                return {"deployment_id": identity}
            if path == "/v1/deployment":
                return dict(receipt(), deployment_id=identity)
            return {"capacity": capacity(kv=None)}

        with patch.object(deployment, "fetch", side_effect=fetch):
            monitor.check_once()
            monitor.check_once()
            self.assertEqual(calls.count("/v1/deployment"), 1)
            identity = "b" * 16
            monitor.check_once()
        self.assertEqual(calls.count("/v1/deployment"), 2)
        self.assertTrue(pool.snapshot()["replicas"][0]["deployment_restarted"])

    def test_completions_and_replacement_during_probe_cannot_inflate_capacity(self):
        for replacement in (False, True):
            with self.subTest(replacement=replacement):
                router, pool, monitor = self.pool()

                def initial(spec, path, timeout, **kwargs):
                    return {} if path == "/health" else None if path == "/v1/deployment" else {"capacity": capacity(total=2)}

                with patch.object(deployment, "fetch", side_effect=initial):
                    monitor.check_once()
                pool.acquire()
                pool.acquire()
                entered, release = threading.Event(), threading.Event()

                def blocked(spec, path, timeout, **kwargs):
                    if path == "/api/status":
                        entered.set()
                        if not release.wait(3):
                            raise ValueError("test probe timed out")
                        # One external request, replacement local work still
                        # in transport and therefore absent from this sample.
                        return {"capacity": capacity(total=2, busy=1 if replacement else 0)}
                    return initial(spec, path, timeout, **kwargs)

                with patch.object(deployment, "fetch", side_effect=blocked):
                    thread = threading.Thread(target=monitor.check_once)
                    thread.start()
                    try:
                        self.assertTrue(entered.wait(2))
                        pool.release("one", ok=True)
                        pool.release("one", ok=True)
                        if replacement:
                            pool.acquire()
                    finally:
                        release.set()
                        thread.join(3)
                self.assertFalse(thread.is_alive())
                if not replacement:
                    self.assertEqual(pool.acquire().id, "one")
                    self.assertEqual(pool.acquire().id, "one")
                with self.assertRaises(router.NoReplicaAvailable):
                    pool.acquire()

    def test_fast_node_is_published_while_other_node_waits(self):
        _, pool, monitor = self.pool(("fast", "slow"))
        slow_started, release, published = threading.Event(), threading.Event(), threading.Event()
        original = pool.report_health

        def report(name, *args, **kwargs):
            original(name, *args, **kwargs)
            if name == "fast":
                published.set()

        def fetch(spec, path, timeout, **kwargs):
            if spec.id == "slow" and path == "/health":
                slow_started.set()
                if not release.wait(3):
                    raise ValueError("test slow probe timed out")
            return {} if path == "/health" else None if path == "/v1/deployment" else {"capacity": capacity()}

        with patch.object(deployment, "fetch", side_effect=fetch), patch.object(pool, "report_health", side_effect=report):
            thread = threading.Thread(target=monitor.check_once)
            thread.start()
            try:
                self.assertTrue(slow_started.wait(2))
                self.assertTrue(published.wait(2))
                self.assertTrue(thread.is_alive())
                self.assertEqual(pool.acquire().id, "fast")
            finally:
                release.set()
                thread.join(3)
        self.assertFalse(thread.is_alive())


@unittest.skipUnless(os.name == "posix", "node inspection owns POSIX child groups")
class BoundedInspectionTests(unittest.TestCase):
    def node(self):
        spec = importlib.util.spec_from_file_location("bounded_deployment", ROOT / "node/hydraweave_deploy.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_drains_both_streams_without_pipe_deadlock(self):
        node = self.node()
        result = node.capture([sys.executable, "-c", "import sys;sys.stderr.write('e'*100000);sys.stdout.write('o'*100000)"],
                              dict(os.environ), 5)
        self.assertEqual((len(result.stdout), len(result.stderr), result.returncode), (100000, 100000, 0))

    def test_oversized_output_and_deadline_reap_owned_children(self):
        node = self.node()
        with self.assertRaisesRegex(ValueError, "exceeds"):
            node.capture([sys.executable, "-c", "import sys;sys.stdout.write('x'*300000)"], dict(os.environ), 5)
        with self.assertRaises(subprocess.TimeoutExpired):
            node.capture([sys.executable, "-c", "import time;time.sleep(30)"], dict(os.environ), 0.1)
