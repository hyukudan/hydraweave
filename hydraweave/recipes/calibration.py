"""Bind externally measured Strix performance inputs to one current solver request.

This module validates provenance and scope. It never measures hardware, invents
rates, authenticates a publisher, or duplicates Tierstream's cost arithmetic.
"""
from copy import deepcopy
import math
from pathlib import Path
import re
import time

from . import content_cache, grants
from .reader import RecipeError

SCHEMA = "hydraweave/performance-calibration-v1"
MAX_AGE_S = 7 * 24 * 3600
MAX_PROBE_AGE_S = 24 * 3600
PERFORMANCE_FIELDS = ("costs", "curves")


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and value.strip().lower() not in ("unknown", "unavailable")


def _number(value, *, positive=False):
    try:
        return type(value) in (int, float) and math.isfinite(value) and (value > 0 if positive else value >= 0)
    except OverflowError:
        return False


def performance(inputs):
    """The exact cost/curve documents whose values need a measurement receipt."""
    if not isinstance(inputs, dict):
        raise RecipeError("PL_E_SCHEMA solver inputs must be a table")
    choices = inputs.get("choices", [])
    if not isinstance(choices, list) or any(not isinstance(c, dict) for c in choices):
        raise RecipeError("PL_E_SCHEMA solver choices must be tables")
    selected = {k: deepcopy(inputs[k]) for k in PERFORMANCE_FIELDS if k in inputs}
    per_choice = [{"index": index, "id": choice.get("id"),
                   **{k: deepcopy(choice[k]) for k in PERFORMANCE_FIELDS if k in choice}}
                  for index, choice in enumerate(choices) if any(k in choice for k in PERFORMANCE_FIELDS)]
    if per_choice:
        selected["choices"] = per_choice
    return selected


def without_performance(inputs):
    performance(inputs)  # Validate before walking optional choices.
    cleaned = deepcopy(inputs)
    for target in [cleaned, *cleaned.get("choices", [])]:
        for field in PERFORMANCE_FIELDS:
            target.pop(field, None)
    return cleaned


def layer_settings(layers):
    import hashlib
    sources = []
    for role, paths in (("profile", [layers.profile] if layers.profile else []), ("overrides", layers.overrides)):
        for path in paths:
            with Path(path).open("rb") as stream:
                raw = stream.read(content_cache.MAX_JSON_BYTES + 1)
            if len(raw) > content_cache.MAX_JSON_BYTES:
                raise RecipeError("PL_E_SCHEMA calibration layer exceeds input limit")
            sources.append({"role": role, "sha256": hashlib.sha256(raw).hexdigest()})
    return {"sets": layers.sets, "layers": sources}


def identity(probe, inputs, *, route, workload, context, recipe_sha256, settings=None):
    """Strict physical/build/workload identity; no default GPU, driver, link or residency is inferred."""
    if not isinstance(probe, dict) or not _text(route) or not _text(workload):
        raise RecipeError("PL_E_STALE calibration needs the current probe, route and workload")
    for field in ("engine_sha256", "model_sha256"):
        if not content_cache._digest(probe.get(field)):
            raise RecipeError("PL_E_STALE calibration needs current " + field)
    if not isinstance(probe.get("engine_revision"), str) or not re.fullmatch(r"[0-9a-f]{40}", probe["engine_revision"]):
        raise RecipeError("PL_E_STALE calibration needs the exact engine revision")
    if not content_cache._digest(recipe_sha256) or not isinstance(context, dict):
        raise RecipeError("PL_E_STALE calibration needs the exact recipe and workload context")
    rows = probe.get("devices")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 128:
        raise RecipeError("PL_E_STALE calibration needs physical device identities")
    devices, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or row.get("present", True) is not True:
            raise RecipeError("PL_E_STALE calibration device is absent")
        if not all(_text(row.get(k)) for k in ("id", "uuid", "driver")):
            raise RecipeError("PL_E_STALE calibration needs physical UUID and current driver for every device")
        uuid = grants.uuid_key(row["uuid"])
        if not (re.fullmatch(r"(?:gpu-)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", uuid)
                and int(uuid.removeprefix("gpu-").replace("-", ""), 16) > 0
                or re.fullmatch(r"amd-[0-9a-f]{1,64}", uuid) and int(uuid[4:], 16) > 0):
            raise RecipeError("PL_E_STALE calibration device UUID is not a physical GPU identity")
        if uuid in seen:
            raise RecipeError("PL_E_STALE calibration physical device is duplicated")
        seen.add(uuid)
        devices.append({k: deepcopy(row[k]) for k in ("id", "host", "driver", "pci", "arch", "platform",
                         "memory_bytes", "memory_gib", "pci_path", "link", "via", "numa_node") if k in row})
        devices[-1]["uuid"] = uuid
    devices.sort(key=lambda d: (d["id"], d["uuid"]))
    links = probe.get("links")
    if not isinstance(links, list):
        raise RecipeError("PL_E_STALE calibration needs an explicit link inventory (empty for no links)")
    for link in links:
        if (not isinstance(link, dict) or not content_cache._digest(link.get("measurement_sha256"))
                or not _number(link.get("bytes_per_s"), positive=True)
                or not _number(link.get("latency_ns"))):
            raise RecipeError("PL_E_STALE calibration needs measured bandwidth and latency for every link")
    residency = probe.get("residency")
    if (not isinstance(residency, dict) or residency.get("mode") not in ("cold", "warm", "mixed")
            or not content_cache._digest(residency.get("state_sha256"))):
        raise RecipeError("PL_E_STALE calibration needs an explicit current model residency snapshot")
    if not _text(probe.get("file_store")):
        raise RecipeError("PL_E_STALE calibration needs current store identity (or explicit not-used)")
    structural = without_performance(inputs)
    # Transfer samples are separately identity-bound and expire independently.
    for target in [structural, *structural.get("choices", [])]:
        target.pop("probe_xfer", None)
    return {"engine_revision": probe["engine_revision"], "engine_sha256": probe["engine_sha256"],
            "model_sha256": probe["model_sha256"], "devices": devices, "route": route, "workload": workload,
            "recipe_sha256": recipe_sha256, "context_sha256": content_cache._sha(context),
            "configuration_sha256": content_cache._sha({"inputs": structural,
                          "settings": settings if settings is not None else {"sets": [], "layers": []}}),
            "links_sha256": content_cache._sha(links),
            "residency_sha256": content_cache._sha(residency), "store_id": probe["file_store"],
            "xfer_sha256": content_cache._sha(probe.get("xfer_sha256", {})),
            "xfer_host_sha256": content_cache._sha(probe.get("xfer_host", {}))}


def receipt(probe, inputs, *, route, workload, context, recipe_sha256, measured_at, expires_at, evidence, settings=None):
    """Package supplied, already measured cost/curve documents. No measurement or timestamp is manufactured."""
    if not performance(inputs):
        raise RecipeError("PL_E_STALE calibration needs measured cost or curve documents")
    document = {"schema": SCHEMA,
                "identity": identity(probe, inputs, route=route, workload=workload, context=context,
                                     recipe_sha256=recipe_sha256, settings=settings),
                "performance_sha256": content_cache._sha(performance(inputs)),
                "measured_at": measured_at, "expires_at": expires_at, "evidence": deepcopy(evidence),
                "extrapolated": False}
    _record(document)
    return document


def _record(document):
    required = {"schema", "identity", "performance_sha256", "measured_at", "expires_at", "evidence", "extrapolated"}
    if not isinstance(document, dict) or set(document) != required or document.get("schema") != SCHEMA:
        raise RecipeError("PL_E_STALE expected a performance-calibration-v1 receipt")
    if document["extrapolated"] is not False:
        raise RecipeError("PL_E_STALE extrapolated performance cannot calibrate this request")
    start, end = document["measured_at"], document["expires_at"]
    if not _number(start, positive=True) or not _number(end, positive=True) or not start < end <= start + MAX_AGE_S:
        raise RecipeError("PL_E_STALE calibration validity must be positive and at most seven days")
    evidence = document["evidence"]
    if (not isinstance(evidence, dict) or set(evidence) != {"method", "runs", "source_sha256"}
            or not _text(evidence["method"]) or type(evidence["runs"]) is not int or not 3 <= evidence["runs"] <= 1_000_000
            or not isinstance(evidence["source_sha256"], list) or not 1 <= len(evidence["source_sha256"]) <= 128
            or not all(content_cache._digest(v) for v in evidence["source_sha256"])):
        raise RecipeError("PL_E_STALE calibration needs a method, at least three runs and source receipt digests")


def validate(document, probe, inputs, *, route, workload, context, recipe_sha256, settings=None, now=None):
    _record(document)
    if not isinstance(probe, dict):
        raise RecipeError("PL_E_STALE calibration needs the current probe")
    if not performance(inputs):
        raise RecipeError("PL_E_STALE calibration needs measured cost or curve documents")
    now = time.time() if now is None else now
    observed = probe.get("observed_at")
    if not _number(now, positive=True) or not document["measured_at"] <= now < document["expires_at"]:
        raise RecipeError("PL_E_STALE calibration has expired or is from the future; provide current measurements")
    if not _number(observed, positive=True) or not 0 <= now - observed <= MAX_PROBE_AGE_S:
        raise RecipeError("PL_E_STALE calibration probe is older than 24 hours or has no valid timestamp")
    current = identity(probe, inputs, route=route, workload=workload, context=context,
                       recipe_sha256=recipe_sha256, settings=settings)
    if document["identity"] != current:
        raise RecipeError("PL_E_STALE calibration belongs to another build, model, device, driver, workload, link or residency")
    if document["performance_sha256"] != content_cache._sha(performance(inputs)):
        raise RecipeError("PL_E_STALE calibrated performance documents changed")
    return {"status": "calibrated", "receipt_sha256": content_cache._sha(document),
            "measured_at": document["measured_at"], "expires_at": document["expires_at"],
            "scope": "exact recorded configuration; no extrapolation", "evidence": deepcopy(document["evidence"])}
