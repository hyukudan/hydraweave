"""Engine-owned deployment plans and the TensorMeld runtime contract bridge.

This module uses only the standard library so the same source can run in a
node's pinned TensorMeld wrapper without installing the control application.
"""
from copy import deepcopy
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
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
    if (not isinstance(devices, list) or not 1 <= len(devices) <= 256
            or not isinstance(ranks, list) or not 1 <= len(ranks) <= 256 or len(ranks) != chosen["ranks"]):
        raise ValueError("deployment: missing measured devices or rank budgets")
    for device in devices:
        if not isinstance(device, dict) or type(device.get("unified")) is not bool:
            raise ValueError("deployment device: missing memory kind")
        total = uint(device.get("mem_total_bytes"), "device.mem_total_bytes")
        free = uint(device.get("mem_free_bytes"), "device.mem_free_bytes")
        if not total or free > total:
            raise ValueError("deployment device: inconsistent measured memory")
    seen, nodes = set(), set()
    for rank in ranks:
        if not isinstance(rank, dict):
            raise ValueError("deployment rank: expected object")
        index, node = uint(rank.get("rank"), "rank"), uint(rank.get("node"), "node")
        if index in seen or node in nodes or index >= len(ranks) or node >= len(devices) or rank.get("fits") is not True:
            raise ValueError("deployment rank: duplicate, missing node or does not fit")
        seen.add(index)
        nodes.add(node)
        for key in ("total_bytes", "free_bytes", "margin_bytes", "budget_bytes", "kv_bytes", "required_bytes", "free_after_bytes"):
            uint(rank.get(key), f"rank.{key}")
        device = devices[node]
        if (rank["total_bytes"] != device["mem_total_bytes"] or rank["free_bytes"] != device["mem_free_bytes"]
                or rank["margin_bytes"] > rank["free_bytes"]
                or rank["budget_bytes"] > rank["free_bytes"] - rank["margin_bytes"]
                or rank["required_bytes"] > rank["budget_bytes"]
                or rank["free_after_bytes"] != rank["budget_bytes"] - rank["required_bytes"]
                or rank["kv_bytes"] > rank["required_bytes"]):
            raise ValueError("deployment rank: inconsistent budget")
        footprint = rank.get("footprint")
        if not isinstance(footprint, dict):
            raise ValueError("deployment rank: missing footprint")
        owners = sum(uint(footprint.get(key), "footprint." + key) for key in ("weights", "staging"))
        if owners + rank["kv_bytes"] > rank["required_bytes"]:
            raise ValueError("deployment rank: footprint exceeds required bytes")
        if "workspace" in footprint:
            owners += uint(footprint["workspace"], "footprint.workspace") + rank["kv_bytes"]
            extra = footprint.get("extra", [])
            if not isinstance(extra, list) or len(extra) > 4096:
                raise ValueError("deployment rank: invalid extra owners")
            for owner in extra:
                if not isinstance(owner, dict) or not isinstance(owner.get("name"), str):
                    raise ValueError("deployment rank: invalid extra owner")
                owners += uint(owner.get("bytes"), "footprint.extra.bytes")
            if owners != rank["required_bytes"]:
                raise ValueError("deployment rank: required bytes do not match owners")
    return deepcopy(value)


def parse_capacity(value):
    if not isinstance(value, dict):
        raise ValueError("capacity: expected object")
    result = {key: uint(value.get(key), "capacity." + key)
              for key in ("slots_total", "slots_busy", "queue_depth")}
    if "kv_bytes_free" not in value:
        raise ValueError("capacity.kv_bytes_free: missing field")
    # Fixed-slot engines have no shared byte allocator. Null is unknown, not exhausted.
    result["kv_bytes_free"] = (None if value["kv_bytes_free"] is None else
                               uint(value["kv_bytes_free"], "capacity.kv_bytes_free"))
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
        self.plan = None
        self.plan_known = False
        self.plan_probe_at = 0.0

    def observe(self, health, plan, status):
        health_id = health.get("deployment_id") if isinstance(health, dict) else None
        parsed = self.plan if plan is self.plan else parse_plan(plan) if plan is not None else None
        plan_id = parsed["deployment_id"] if parsed is not None else None
        if health_id is not None and (not isinstance(health_id, str) or len(health_id) != 16
                                     or any(c not in "0123456789abcdef" for c in health_id)):
            raise ValueError("invalid health deployment_id")
        identity = health_id or plan_id
        if health_id and health_id != plan_id:
            self.error = "health/deployment identity mismatch"
            return False
        if self.deployment_id is not None and identity != self.deployment_id:
            self.restart = True
        if identity is not None and self.deployment_id is None:
            self.deployment_id = identity
        self.capacity = parse_capacity(status["capacity"]) if isinstance(status, dict) and "capacity" in status else None
        self.plan = parsed
        self.plan_known = True
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
        return min(configured, c["slots_total"], inflight + free)


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
            specs = self.pool.specs()
            started = time.monotonic()
            # Reserve against the local count before the sample. Adding the count
            # at completion would give back requests admitted during the probe.
            with self.pool._cond:
                baseline = {spec.id: (self.pool._by_id(spec.id).inflight, self.pool._by_id(spec.id).assigned)
                            for spec in specs}
            replies, errors = {spec.id: {} for spec in specs}, {}
            remaining = {spec.id: 2 for spec in specs}
            timeout = self.pool.config.health_timeout_s
            with ThreadPoolExecutor(max_workers=min(8, max(1, len(specs) * 3))) as executor:
                pending = {executor.submit(fetch, spec, path, timeout, optional=path != "/health"): (spec, path)
                           for spec in specs for path in ("/health", "/api/status")}
                while pending:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        spec, path = pending.pop(future)
                        contract = self.contracts[spec.id]
                        try:
                            value = future.result()
                            replies[spec.id][path] = value
                            if path == "/health":
                                identity = value.get("deployment_id") if isinstance(value, dict) else None
                                # Native plans are immutable for their live ID.
                                # Re-read on a new ID; legacy absence is re-probed
                                # periodically, not transferred every health tick.
                                refresh = not contract.plan_known or (
                                    not contract.restart and (identity != contract.deployment_id or
                                    (identity is None and time.monotonic() - contract.plan_probe_at >= 30)))
                                if refresh:
                                    task = executor.submit(fetch, spec, "/v1/deployment", timeout, optional=True)
                                    pending[task] = (spec, "/v1/deployment")
                                    remaining[spec.id] += 1
                                else:
                                    replies[spec.id]["/v1/deployment"] = contract.plan
                            elif path == "/v1/deployment":
                                contract.plan_probe_at = time.monotonic()
                        except (OSError, ValueError, http.client.HTTPException) as exc:
                            errors[spec.id] = str(exc)
                        remaining[spec.id] -= 1
                        if remaining[spec.id]:
                            continue
                        # Publish a complete node sample without waiting for
                        # another node's slow health/status call.
                        row = replies.pop(spec.id)
                        self._publish(spec, row, errors.get(spec.id, ""), baseline[spec.id], started)
            if self.services is not None:
                service_specs = self.services.specs()
                with ThreadPoolExecutor(max_workers=min(8, max(1, len(service_specs)))) as executor:
                    pending = {executor.submit(router.probe_replica_health, spec, timeout): spec for spec in service_specs}
                    for future in as_completed(pending):
                        spec = pending[future]
                        ok, error = future.result()
                        self.services.report_health(spec.id, ok, elapsed_ms=(time.monotonic() - started) * 1000, error=error)

        def _publish(self, spec, replies, error, baseline, started):
            ok = False
            contract = self.contracts[spec.id]
            if not error:
                try:
                    ok = contract.observe(replies["/health"], replies["/v1/deployment"], replies["/api/status"])
                    error = contract.error
                except ValueError as exc:
                    error = str(exc)
            if error:
                contract.error = error
            with self.pool._cond:
                replica = self.pool._by_id(spec.id)
                # Counts alone cannot distinguish old work from a replacement
                # admitted during the sample. Subtract every new assignment;
                # completions never manufacture extra credit from a stale poll.
                assigned = max(0, replica.assigned - baseline[1])
                old_inflight = max(0, min(baseline[0], replica.inflight - assigned))
                limit = contract.limit(self.limits[spec.id], old_inflight) if ok else replica.inflight
                replica.spec = replace(replica.spec, max_inflight=limit)
                # Capacity and identity failures stop admission immediately.
                if not ok:
                    replica.up = False
                self.pool._cond.notify_all()
            self.pool.report_health(spec.id, ok, elapsed_ms=(time.monotonic() - started) * 1000, error=error)

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
