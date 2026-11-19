"""Explicit acquisitions and passive local measurement lookup. A cache miss never starts work.

Tierstream owns the xfer-v1 hash. This module accepts that identity rather than duplicating its arithmetic.
GPU work lives in an owned child, blocked on stdin until locks, gates and attributable watchdog are ready.
"""
from __future__ import annotations

from contextlib import ExitStack
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

from .. import gpu_counters, pc_big, placement_supervision, procs, rtx
from ..winproc import no_window
from . import content_cache, grants, probe
from .reader import RecipeError

RUNS = 9
XFER_METHOD = {"aggregation": "median", "runs": RUNS, "copies": "pinned", "bytes": 64 << 20,
               "latency_bytes": 4096, "timing": "synchronized-host-monotonic", "readers": 1}
STORE_METHOD = {"aggregation": "median", "runs": RUNS, "io": "direct", "read_bytes": probe.DISK_CHUNK,
                "readers": 1, "max_bytes_per_run": probe.DISK_PROBE_DEFAULT_BYTES}
MAX_AGE_S = 24 * 3600
CACHE_SCHEMA = "hydraweave/measurement-v2"


def key(uuid, driver, xfer_sha256, store_id):
    if (not all(isinstance(v, str) and v and v != "unknown" for v in (uuid, driver, store_id))
            or not content_cache._digest(xfer_sha256)):
        raise RecipeError("PL_E_STALE measurements need UUID, driver, xfer-v1 SHA and store id")
    return {"uuid": grants.uuid_key(uuid), "driver": driver, "xfer_sha256": xfer_sha256, "store_id": store_id}


class Cache:
    def __init__(self, runtime, *, clock=time.time):
        self.directory = Path(runtime) / "measurements"
        self.clock = clock

    def save(self, identity, mode, result, *, measured_at=None):
        identity = key(**identity)
        method = _method(mode)
        try:
            if result != median(result["provenance"]["samples"], method) or result["provenance"]["method"] != method:
                raise RecipeError("PL_E_STALE measurement does not match its samples and method")
        except (KeyError, TypeError) as exc:
            raise RecipeError("PL_E_STALE invalid measurement provenance") from exc
        measured_at = self.clock() if measured_at is None else measured_at
        if not _timestamp(measured_at):
            raise RecipeError("PL_E_STALE measurement timestamp is required")
        content_cache._write(self.directory / (content_cache._sha([identity, mode]) + ".json"),
                             {"schema": CACHE_SCHEMA, "key": identity, "mode": mode, "result": result,
                              "measured_at": measured_at, "expires_at": measured_at + MAX_AGE_S})

    def lookup(self, identity, mode):
        """Read JSON only. Changed, expired or legacy evidence is unpredicted, never refreshed."""
        try:
            identity = key(**identity)
            row = content_cache._read(self.directory / (content_cache._sha([identity, mode]) + ".json"), "PL_E_STALE")
            result = row["result"]
            provenance = result["provenance"]
            method, now = _method(mode), self.clock()
            if (row.get("schema") != CACHE_SCHEMA or row.get("key") != identity or row.get("mode") != mode
                    or provenance["method"] != method or median(provenance["samples"], method) != result):
                raise RecipeError("measurement identity, samples or schema changed")
            measured, expires = row.get("measured_at"), row.get("expires_at")
            if (not all(_timestamp(v) for v in (measured, expires, now))
                    or not measured <= now < expires <= measured + MAX_AGE_S):
                return {"status": "unpredicted", "why": "measurement expired or timestamp invalid; run an explicit probe"}
            return {"status": "measured", "measured_at": measured, "expires_at": expires, **result}
        except (RecipeError, KeyError, TypeError, ValueError):
            return {"status": "unpredicted", "why": "no valid measurement for the current identity; run an explicit probe"}


def _timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _method(mode):
    if mode not in ("xfer", "store"):
        raise RecipeError("PL_E_STALE unknown measurement mode")
    return XFER_METHOD if mode == "xfer" else STORE_METHOD


def bind_keys(probe_document, keys):
    """An absent current driver/store/xfer identity cannot be filled by an old cache key."""
    if not isinstance(probe_document, dict) or not isinstance(keys, dict):
        raise RecipeError("PL_E_SCHEMA measurement probe and keys must be tables")
    devices = probe_document.get("devices", [])
    if not isinstance(devices, list):
        raise RecipeError("PL_E_SCHEMA measurement probe devices must be a list")
    current = {}
    for sid, supplied in keys.items():
        if (not isinstance(sid, str) or not sid or not isinstance(supplied, dict)
                or set(supplied) != {"uuid", "driver", "xfer_sha256", "store_id"}):
            raise RecipeError("PL_E_SCHEMA invalid measurement key")
        identity = dict(supplied)
        if sid == "store":
            identity.update(uuid="host-store", driver="none")
        else:
            matches = [d for d in devices if isinstance(d, dict)
                       and isinstance(d.get("uuid"), str) and isinstance(identity["uuid"], str)
                       and grants.uuid_key(d["uuid"]) == grants.uuid_key(identity["uuid"])
                       and d.get("present", True) is True]
            identity["driver"] = matches[0].get("driver", "unknown") if len(matches) == 1 else "unknown"
        identity["store_id"] = probe_document.get("file_store") or "unknown"
        transfers = probe_document.get("xfer_sha256")
        identity["xfer_sha256"] = (transfers.get(sid) or "unknown") if isinstance(transfers, dict) else "unknown"
        current[sid] = identity
    return current


def approved(window, *, now):
    if not isinstance(window, dict) or not isinstance(window.get("approval"), str) or not window["approval"].strip():
        raise RecipeError("PL_E_GRANT store/manifest reads need an operator-approved window")
    interval = {"starts_at": window.get("starts_at"), "expires_at": window.get("expires_at"), "kind": "device"}
    grants.parse_window(interval, "probe.window")
    if not grants._active(interval, now):
        raise RecipeError("PL_E_GRANT probe window is not open or has expired")
    return rtx.expires(interval["expires_at"], "probe.window.expires_at")


def gpu_grant(cluster, row, *, now, guard=None):
    cuda = cluster.pc.get("cuda", {})
    entries = grants.parse_grants(cuda.get("grants", []))
    grant = next((g for g in entries if grants.uuid_key(g["uuid"]) == grants.uuid_key(row["uuid"])), None)
    if grant is None or not grants._active(grant["window"], now, starting=True) or grant["vram_cap_gib"] <= 0:
        raise RecipeError("PL_E_GRANT measurement requires a current grant for this UUID (lab mode included)")
    deadline = grants.pc_big_deadline(cluster, [row], now=now, guard=guard)
    if deadline is not None and deadline <= now + 60:
        raise RecipeError("PL_E_GRANT no measurement during a PC-BIG window")
    return grant


def median(samples, method):
    fields = ({"h2d_mbps", "d2h_mbps", "h2d_lat_ns"} if method == XFER_METHOD else
              {"file_read_mbps"} if method == STORE_METHOD else None)
    if (fields is None or not isinstance(samples, list) or len(samples) != RUNS
            or any(not isinstance(s, dict) or set(s) != fields for s in samples)):
        raise RecipeError("PL_E_STALE measurement needs nine runs of the same method")
    if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for s in samples for v in s.values()):
        raise RecipeError("PL_E_STALE invalid measurement sample")
    return {"values": {k: statistics.median(s[k] for s in samples) for k in samples[0]},
            "provenance": {"method": method, "samples": samples}}


def _gpu(row, grant, *, clock=time.time, lock_factory=rtx.device_lock, gate_runner=rtx.run_gate,
         observer_factory=gpu_counters.Observer, watchdog_factory=placement_supervision.Watchdog,
         popen=subprocess.Popen, job_factory=None):
    policy = {"grants": [grant], "caps_gib": {grant["uuid"]: grant["vram_cap_gib"]},
              "caps_bytes": {grants.uuid_key(grant["uuid"]): int(grant["vram_cap_gib"] * rtx.GIB)},
              "host_pinned_gib": 0, "host_pageable_gib": 0, "free_commit_floor_gib": 4,
              "pc_big_guard": row.get("_pc_big_guard")}
    end = min(clock() + 60, rtx.expires(grant["window"]["expires_at"], "probe grant"))
    spec = SimpleNamespace(placement_supervision=policy, deadline=end, free_commit_floor_gib=4, ready_timeout_s=0)
    with ExitStack() as stack:
        placement_supervision.validate(policy, now=clock(), starting=True, memory=rtx.memory_status())
        placement_supervision.acquire(policy, stack, lock_factory=lock_factory, gate_runner=gate_runner,
                                      verify=lambda u: _idle(u), log=lambda _: None, now=clock())
        observer = observer_factory()
        owned = [True]
        stack.callback(lambda: observer.close() if owned[0] else None)
        placement_supervision.validate(policy, now=clock(), starting=True, observer=observer,
                                      owners={grants.uuid_key(grant["uuid"]): {os.getpid()}})
        sample = observer.sample(os.getpid())
        if (grants.uuid_key(row["uuid"]) not in sample
                or any(type(v) is not int or v < 0 for v in sample.values())):
            raise RecipeError("PL_E_CAPABILITY measurement observer cannot attribute the selected GPU")
        env = dict(os.environ, CUDA_VISIBLE_DEVICES=row["uuid"], CUDA_DEVICE_ORDER="PCI_BUS_ID")
        job = (job_factory or procs._WindowsJob)() if os.name == "nt" or job_factory else None
        if job is not None:
            stack.callback(job.close)
        child = popen([sys.executable, "-m", "hydraweave.recipes.measure_worker"],
                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                      text=True, env=env, **no_window())
        def cleanup():
            if child.poll() is None:
                child.kill()
            child.communicate()
        stack.callback(cleanup)
        if job is not None:
            job.assign(child)  # Worker is still blocked on stdin; parent death closes the Job.
        dog = watchdog_factory(spec, child.pid, child.kill, lambda _: None, observer=observer, clock=clock)
        owned[0] = False  # Watchdog.start closes its observer on failure too.
        dog.start()
        # Watchdog owns the observer from here; clean up even when communicate/JSON validation fails.
        try:
            request = {"uuid": row["uuid"], "parent_pid": os.getpid(), "deadline": end}
            out, error = child.communicate(json.dumps(request) + "\n", timeout=max(.01, end - clock()))
            reason = dog.reason or dog.check()
            if reason or child.returncode:
                raise RecipeError("PL_E_GRANT probe stopped: " + (reason or error.strip()))
            return median(json.loads(out), XFER_METHOD)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate()
            dog.stop()


def _idle(uuid):
    busy = rtx.pro_busy(uuid)
    if busy:
        raise RecipeError("PL_E_BUSY measurement device is busy: " + busy)


def _store(path, window, *, clock=time.time, measure=probe.read_bandwidth):
    samples = []
    for _ in range(RUNS):
        remaining = approved(window, now=clock()) - clock()
        result = measure(path, max_s=min(probe.DISK_PROBE_MAX_S, remaining))
        if result["method"] != "direct":
            raise RecipeError("PL_E_STALE buffered store reads cannot predict FILE performance")
        approved(window, now=clock())
        samples.append({"file_read_mbps": result["gbytes_ps"] * 1000})
    return median(samples, STORE_METHOD)


def manifest(cluster, files, window, *, clock=time.time, run=subprocess.run):
    """Full-read CPU acquisition owned by ts-recipe, bounded by the operator's approved deadline."""
    from . import core
    end = approved(window, now=clock())
    executable = core.locate(cluster)
    if executable is None:
        raise RecipeError("PL_E_CAPABILITY probe --manifest requires ts-recipe")
    argv = [str(executable), "manifest", "--full", *[str(Path(p).resolve()) for p in files]]
    remaining = end - clock()
    if remaining <= 0:
        raise RecipeError("PL_E_GRANT manifest window has expired")
    with tempfile.TemporaryFile("w+b") as stdout, tempfile.TemporaryFile("w+b") as stderr:
        try:
            result = run(argv, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, timeout=remaining, **no_window())
        except subprocess.TimeoutExpired as exc:
            raise RecipeError("PL_E_GRANT manifest stopped at the approved window deadline") from exc
        except OSError as exc:
            raise RecipeError("PL_E_CAPABILITY ts-recipe manifest unavailable") from exc
        approved(window, now=clock())
        stdout.seek(0)
        stderr.seek(0)
        text = core._bounded(stdout, "manifest output", "ts-recipe", limit=content_cache.MAX_JSON_BYTES)
        err = core._bounded(stderr, "manifest error output", "ts-recipe", limit=content_cache.MAX_JSON_BYTES)
    if result.returncode:
        raise RecipeError(err.rstrip() or "PL_E_STALE ts-recipe manifest refused")
    try:
        document = json.loads(text)
        entries = content_cache.ManifestStore._entries(document)
        if set(entries) != {os.path.normcase(str(Path(p).resolve())) for p in files}:
            raise ValueError("manifest differs from requested files")
    except (ValueError, TypeError) as exc:
        raise RecipeError("PL_E_STALE invalid ts-recipe manifest output") from exc
    return document


def acquire(cluster, mode, *, runtime, uuid=None, xfer_sha256=None, store_id=None, files=(), window=None,
            clock=time.time, inventory=None, gpu_runner=_gpu, store_runner=_store,
            manifest_runner=None):
    """Explicit top-level command only. All authorization checks precede any child or model read."""
    if mode not in ("xfer", "store", "manifest"):
        raise RecipeError("unknown measurement")
    approval = None
    if mode != "xfer":
        if window is None:
            approved(None, now=clock())
        approval = content_cache._read(window, "PL_E_GRANT") if isinstance(window, (str, Path)) else window
        approved(approval, now=clock())
        if not files or (mode == "store" and len(files) != 1):
            raise RecipeError("probe needs explicit --file paths (exactly one for --store)")
    if mode == "manifest":
        # The core owns header/table boundaries and the manifest schema; no substitute hash algorithm here.
        if manifest_runner is None:
            raise RecipeError("PL_E_CAPABILITY ts-recipe manifest integration requires a qualified manifest CLI")
        document = manifest_runner(files, approval)
        approved(approval, now=clock())
        return {"manifest_sha256": content_cache.ManifestStore(runtime).save(document)}
    if mode == "xfer":
        configured = [d for m in cluster.machines.values() if m.access == "local" for d in m.devices
                      if not d.get("excluded", False) and uuid and grants.uuid_key(d.get("uuid", "")) == grants.uuid_key(uuid)]
        if len(configured) != 1:
            raise RecipeError("PL_E_UNKNOWN_DEVICE probe UUID must be configured and non-excluded")
        # Refuse missing grants before even querying inventory.
        try:
            guard = pc_big.context(cluster, [{"uuid": uuid}])
        except rtx.ServiceError as exc:
            raise RecipeError("PL_E_GRANT " + str(exc)) from exc
        gpu_grant(cluster, {"uuid": uuid, "name": ""}, now=clock(), guard=guard)
        inventory_reader = inventory if inventory is not None else lambda: probe.nvidia_gpus(placement=True)
        rows = [r for r in inventory_reader()
                if grants.uuid_key(r["uuid"]) == grants.uuid_key(uuid)]
        if len(rows) != 1:
            raise RecipeError("PL_E_UNKNOWN_DEVICE measurement UUID is absent or ambiguous")
        row = dict(rows[0], _pc_big_guard=guard)
        grant = gpu_grant(cluster, row, now=clock(), guard=guard)
        identity = key(row["uuid"], row.get("driver"), xfer_sha256, store_id)
        result = gpu_runner(row, grant)
        gpu_grant(cluster, row, now=clock(), guard=guard)
        current = [r for r in inventory_reader() if grants.uuid_key(r["uuid"]) == grants.uuid_key(uuid)]
        if len(current) != 1 or current[0].get("driver") != row.get("driver"):
            raise RecipeError("PL_E_STALE GPU identity or driver changed during measurement")
    else:
        # Store figures are host-only; the explicit sentinel keeps GPU and store identities disjoint.
        identity = key("host-store", "none", xfer_sha256, store_id)
        before = probe.file_stat(files[0])
        if before is None:
            raise RecipeError("PL_E_STALE store measurement file is unavailable")
        result = store_runner(files[0], approval)
        approved(approval, now=clock())
        if before != probe.file_stat(files[0]):
            raise RecipeError("PL_E_STALE store file changed during measurement")
    Cache(runtime, clock=clock).save(identity, mode, result)
    return {"key": identity, **result}
