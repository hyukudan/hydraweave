"""Engine-owned deployment plans and the TensorMeld runtime contract bridge.

This module uses only the standard library so the same source can run in a
node's pinned TensorMeld wrapper without installing the control application.
"""
from copy import deepcopy
from dataclasses import replace
import http.client
import json
import time


SCHEMA = "strix-deployment-v1"
MAX_JSON = 256 * 1024


def uint(value, where):
    if type(value) is not int or not 0 <= value < 1 << 64:
        raise ValueError(f"{where}: expected nonnegative integer bytes/count")
    return value


def parse_plan(value):
    """Validate the placement receipt; never infer usable UMA from VRAM + GTT."""
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ValueError(f"expected schema {SCHEMA}")
    if value.get("refused") is not False:
        raise ValueError("deployment refused: " + str(value.get("refusal", "missing admission decision")))
    identity = value.get("deployment_id")
    if not isinstance(identity, str) or len(identity) != 16 or any(c not in "0123456789abcdef" for c in identity):
        raise ValueError("deployment_id: expected 16 lowercase hex digits")
    chosen = value.get("chosen")
    if not isinstance(chosen, dict) or not isinstance(chosen.get("topology"), str) or not isinstance(chosen.get("format"), dict):
        raise ValueError("deployment chosen: missing topology/format")
    for key in ("context", "slots", "ranks"):
        if not uint(chosen.get(key), f"chosen.{key}"):
            raise ValueError(f"chosen.{key}: must be positive")
    devices, ranks = value.get("devices"), value.get("ranks")
    if not isinstance(devices, list) or not devices or not isinstance(ranks, list) or len(ranks) != chosen["ranks"]:
        raise ValueError("deployment: missing measured devices or rank budgets")
    for device in devices:
        if not isinstance(device, dict) or type(device.get("unified")) is not bool:
            raise ValueError("deployment device: missing memory kind")
        total = uint(device.get("mem_total_bytes"), "device.mem_total_bytes")
        free = uint(device.get("mem_free_bytes"), "device.mem_free_bytes")
        if not total or free > total:
            raise ValueError("deployment device: inconsistent measured memory")
    seen = set()
    for rank in ranks:
        if not isinstance(rank, dict):
            raise ValueError("deployment rank: expected object")
        index, node = uint(rank.get("rank"), "rank"), uint(rank.get("node"), "node")
        if index in seen or index >= len(ranks) or node >= len(devices) or rank.get("fits") is not True:
            raise ValueError("deployment rank: duplicate, missing node or does not fit")
        seen.add(index)
        for key in ("total_bytes", "free_bytes", "margin_bytes", "budget_bytes", "kv_bytes", "required_bytes", "free_after_bytes"):
            uint(rank.get(key), f"rank.{key}")
        if (rank["free_bytes"] > rank["total_bytes"] or rank["budget_bytes"] > rank["free_bytes"]
                or rank["required_bytes"] > rank["budget_bytes"]
                or rank["free_after_bytes"] > rank["budget_bytes"] - rank["required_bytes"]):
            raise ValueError("deployment rank: inconsistent budget")
    return deepcopy(value)


def parse_capacity(value):
    if not isinstance(value, dict):
        raise ValueError("capacity: expected object")
    result = {key: uint(value.get(key), "capacity." + key)
              for key in ("slots_total", "slots_busy", "kv_bytes_free", "queue_depth")}
    if result["slots_busy"] > result["slots_total"]:
        raise ValueError("capacity: busy slots exceed total")
    return result


class EngineContract:
    """A deployment identity is sticky until an explicit router reload."""
    def __init__(self):
        self.deployment_id = None
        self.restart = False
        self.capacity = None
        self.error = ""

    def observe(self, health, plan, status):
        health_id = health.get("deployment_id") if isinstance(health, dict) else None
        plan_id = parse_plan(plan)["deployment_id"] if plan is not None else None
        if health_id is not None and (not isinstance(health_id, str) or not health_id):
            raise ValueError("invalid health deployment_id")
        identity = health_id or plan_id
        if health_id and plan_id and health_id != plan_id:
            self.error = "health/deployment identity mismatch"
            return False
        if self.deployment_id is not None and identity != self.deployment_id:
            self.restart = True
        if identity is not None and self.deployment_id is None:
            self.deployment_id = identity
        self.capacity = parse_capacity(status["capacity"]) if isinstance(status, dict) and "capacity" in status else None
        self.error = "deployment changed; reload the router" if self.restart else ""
        return not self.restart

    def limit(self, configured, inflight):
        if self.restart or self.error:
            return inflight
        if self.capacity is None:
            return configured
        c = self.capacity
        free = max(0, c["slots_total"] - c["slots_busy"] - c["queue_depth"])
        if c["kv_bytes_free"] == 0:
            free = 0
        # Busy already includes this router's running work. Add local in-flight
        # once, then let TensorMeld account for reservations after this sample.
        return min(configured, inflight + free)


def fetch(spec, path, timeout, *, optional=False):
    conn = http.client.HTTPConnection(spec.address, spec.port, timeout=timeout)
    try:
        headers = {"Connection": "close"}
        if spec.api_key:
            headers["Authorization"] = "Bearer " + spec.api_key
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        body = response.read(MAX_JSON + 1)
        if optional and response.status in (404, 405, 501):
            return None
        if response.status != 200 or len(body) > MAX_JSON:
            raise ValueError(f"{path}: HTTP {response.status} or oversized JSON")
        try:
            return json.loads(body)
        except ValueError:
            if path == "/health":  # Older engines return plain text 'ok'.
                return {}
            raise
    finally:
        conn.close()


def install_router_bridge(router):
    """Extend the pinned router through its pool/monitor classes, not its files."""
    base = router.HealthMonitor
    pool_base = router.ReplicaPool

    class ContractPool(pool_base):
        def snapshot(self):
            value = super().snapshot()
            for row in value["replicas"]:
                contract = getattr(self, "_deployment_contracts", {}).get(row["id"])
                if contract is not None:
                    row.update(deployment_id=contract.deployment_id, deployment_restarted=contract.restart,
                               capacity=contract.capacity, deployment_error=contract.error)
            return value

    class ContractMonitor(base):
        def __init__(self, pool, services=None):
            super().__init__(pool, services)
            self.contracts = {spec.id: EngineContract() for spec in pool.specs()}
            pool._deployment_contracts = self.contracts
            self.limits = {spec.id: spec.max_inflight for spec in pool.specs()}

        def check_once(self):
            for spec in self.pool.specs():
                started, error, ok = time.monotonic(), "", False
                contract = self.contracts[spec.id]
                try:
                    timeout = self.pool.config.health_timeout_s
                    health = fetch(spec, "/health", timeout)
                    plan = fetch(spec, "/v1/deployment", timeout, optional=True)
                    status = fetch(spec, "/api/status", timeout, optional=True)
                    ok = contract.observe(health, plan, status)
                    error = contract.error
                except (OSError, ValueError, http.client.HTTPException) as exc:
                    error = str(exc)
                with self.pool._cond:
                    replica = self.pool._by_id(spec.id)
                    limit = contract.limit(self.limits[spec.id], replica.inflight) if ok else replica.inflight
                    replica.spec = replace(replica.spec, max_inflight=limit)
                    # Capacity and identity failures stop admission immediately.
                    if not ok:
                        replica.up = False
                    self.pool._cond.notify_all()
                self.pool.report_health(spec.id, ok, elapsed_ms=(time.monotonic() - started) * 1000, error=error)
            if self.services is not None:
                for spec in self.services.specs():
                    started = time.monotonic()
                    ok, error = router.probe_replica_health(spec, self.pool.config.health_timeout_s)
                    self.services.report_health(spec.id, ok, elapsed_ms=(time.monotonic() - started) * 1000, error=error)

    router.ReplicaPool = ContractPool
    router.HealthMonitor = ContractMonitor


def router_main(arguments):
    import runpy
    import sys
    from tensormeld import replica_router
    install_router_bridge(replica_router)
    sys.argv = ["tensormeld", *arguments]
    runpy.run_module("tensormeld", run_name="__main__")


if __name__ == "__main__":
    import sys
    router_main(sys.argv[1:])
