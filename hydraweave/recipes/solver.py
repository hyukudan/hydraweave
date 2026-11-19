"""Operator-selected Tierstream rows, snapshotted locally and saved only through the core's pick/re-plan API."""
from __future__ import annotations

from ..compat import schema as compat_schema
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading

from . import calibration, commands, content_cache, core, placement, reader, service, session
from .reader import RecipeError

SCHEMA = "hydraweave/solver-selection-v1"
MAX_PLAN_FILES = 64
MAX_PLAN_BYTES = 64 * 1024 * 1024
_plan_lock = threading.Lock()


def _save_plan(digest, record):
    """Bound local selections by count and encoded bytes; evict oldest writes first."""
    size = len(content_cache._json(record))
    if size > MAX_PLAN_BYTES or size > content_cache.MAX_JSON_BYTES:
        raise RecipeError("PL_E_SCHEMA solver plan exceeds cache size limit")
    directory = commands.runtime_dir() / "solver-plans"
    with _plan_lock, service.runtime_lock():
        files = sorted(directory.glob("*.json"), key=lambda p: (p.stat().st_mtime_ns, p.name))
        target = directory / (digest + ".json")
        files = [p for p in files if p != target]
        sizes = {p: p.stat().st_size for p in files}
        total = sum(sizes.values()) + size
        while files and (len(files) >= MAX_PLAN_FILES or total > MAX_PLAN_BYTES):
            oldest = files.pop(0)
            oldest.unlink()
            total -= sizes[oldest]
        content_cache._write(target, record)


def rows(document):
    """Display all core rows in their original order, including refusals and unpredicted estimates."""
    if compat_schema(document.get("schema")) != "tierstream/solver-v1" or not isinstance(document.get("rows"), list):
        raise RecipeError("PL_E_SCHEMA expected tierstream/solver-v1 rows")
    result = []
    for row in document["rows"]:
        placed = row.get("placement") or {}
        refusal = placed.get("refusal")
        prediction = row.get("prediction") or {}
        requires = row.get("requires") or []
        extrapolated = (prediction.get("extrapolated") is True or row.get("extrapolated") is True
                        or "extrapolat" in str(prediction.get("tag", "")).lower())
        result.append(dict(deepcopy(row), id=row.get("id") or (row.get("choice") or {}).get("id"), refusal=refusal,
                           estimates=prediction.get("estimates", row.get("estimates", [])),
                           tag=prediction.get("tag", "unpredicted"),
                           saveable=not refusal and not requires and not extrapolated,
                           save_refused=("row refused" if refusal else "requires: " + ", ".join(requires)
                                         if requires else "extrapolated performance needs current calibration"
                                         if extrapolated else None)))
    return result


def _bundle(name):
    service.check_id(name, "solver input bundle")
    return commands.runtime_dir() / "solver-inputs" / (name + ".json")


def _materialize(value, sources, *, directory, depth=0):
    """Freeze JSON document references before planning. Never read models or acquire descriptors/measurements."""
    if depth > 32:
        raise RecipeError("PL_E_SCHEMA solver JSON references are cyclic or too deeply nested")
    def materialize(value, directory=directory):
        return _materialize(value, sources, directory=directory, depth=depth + 1)
    if isinstance(value, dict):
        return {k: {lang: materialize(doc) for lang, doc in v.items()}
            if k == "usage_lang" and isinstance(v, dict) else
            materialize(v) if k in (
            "components", "desc", "context", "usage", "costs", "curves") else
            [materialize(v_) for v_ in v] if k == "choices" and isinstance(v, list)
            else deepcopy(v) for k, v in value.items()}
    if isinstance(value, str):
        path = Path(value)
        path = path if path.is_absolute() else directory / path
        return materialize(_read_source(path, sources), directory=path.parent)
    return deepcopy(value)


def _read_source(path, sources):
    with Path(path).open("rb") as stream:
        raw = stream.read(content_cache.MAX_JSON_BYTES + 1)
    if len(raw) > content_cache.MAX_JSON_BYTES:
        raise RecipeError("PL_E_SCHEMA solver JSON input too large")
    doc = json.loads(raw)
    if not isinstance(doc, dict):
        raise RecipeError("PL_E_SCHEMA solver JSON input must be an object")
    sources[str(Path(path).resolve())] = hashlib.sha256(raw).hexdigest()
    return doc


def _capture(cluster, ref, route, bundle, layers):
    path = service.locate(ref, cluster)
    bundle_path = _bundle(bundle)
    recipe_bytes = path.read_bytes()
    sources = {str(path.resolve()): hashlib.sha256(recipe_bytes).hexdigest()}
    request = _read_source(bundle_path, sources)
    if set(request) - {"probe", "inputs", "desc", "context", "identity", "measurement_keys", "calibration"} or not {"probe", "inputs"} <= request.keys():
        raise RecipeError("PL_E_SCHEMA solver input bundle needs probe and inputs")
    for key in ("probe", "inputs", "desc", "context", "identity", "measurement_keys", "calibration"):
        if key in request:
            request[key] = _materialize(request[key], sources, directory=bundle_path.parent)
    if not isinstance(request["probe"], dict) or not isinstance(request["inputs"], dict):
        raise RecipeError("PL_E_SCHEMA solver probe and inputs must be tables")
    calibration.performance(request["inputs"])
    performance_receipt = request.pop("calibration", None)
    measurement_keys = request.pop("measurement_keys", {})
    measurement_probe = deepcopy(request["probe"])
    measurement_keys = _measurement_keys(measurement_probe, measurement_keys)
    request["probe"] = placement.cached_measurements(measurement_probe, measurement_keys)
    for choice in request["inputs"].get("choices", []):
        choice["probe_xfer"] = deepcopy(request["probe"]["probe_xfer"])
    # Cache references are resolved here, not reopened in a later pick against another manifest/configuration.
    cached_components = []
    for choice in request["inputs"].get("choices", []):
        if content_cache.is_reference(choice.get("components")):
            component_ref, selected = choice["components"], choice.pop("selected_content", None)
            choice["components"] = commands.components_store().load(component_ref, selected=selected)
            cached_components.append({"reference": component_ref, "selected": selected, "document": choice["components"]})
    layer_docs = []
    for role, paths in (("profile", [layers.profile] if layers.profile else []), ("overrides", layers.overrides)):
        for layer in paths:
            layer = Path(layer)
            data = layer.read_bytes()
            sources[str(layer.resolve())] = hashlib.sha256(data).hexdigest()
            layer_docs.append({"role": role, "text": data.decode("utf-8"), "suffix": layer.suffix})
    if performance_receipt is not None:
        calibration_status = calibration.validate(performance_receipt, request["probe"], request["inputs"],
            route=route, workload=layers.workload, context=request.get("context") or {},
            recipe_sha256=hashlib.sha256(recipe_bytes).hexdigest(), settings=calibration.layer_settings(layers))
    else:
        figures = calibration.performance(request["inputs"])
        request["inputs"] = calibration.without_performance(request["inputs"])
        calibration_status = {"status": "unpredicted", "withheld": sorted(figures),
                              "why": "costs and curves require an exact current calibration receipt" if figures
                              else "no calibrated performance inputs were supplied"}
    return {"schema": SCHEMA, "recipe": recipe_bytes.decode("utf-8"), "recipe_ref": str(ref), "route": route,
            "request": request, "sources": sources, "layers": layer_docs, "sets": layers.sets,
            "workload": layers.workload, "config_sha256": service.config_sha256(),
            "cached_components": cached_components, "measurement_keys": measurement_keys,
            "measurement_probe": measurement_probe, "performance_receipt": performance_receipt,
            "calibration": calibration_status}


def _measurement_keys(probe, keys):
    """Bind core-supplied xfer identities to the current probe's physical GPU/driver and store identity."""
    from .measurements import bind_keys
    return bind_keys(probe, keys)


def _check(snapshot):
    if "calibration" not in snapshot:
        raise RecipeError("PL_E_STALE solver evidence format changed; solve again")
    if snapshot.get("config_sha256") != service.config_sha256():
        raise RecipeError("PL_E_STALE solver configuration changed; solve again")
    for path, expected in snapshot["sources"].items():
        try:
            current = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except OSError as exc:
            raise RecipeError("PL_E_STALE solver inputs unavailable; solve again") from exc
        if current != expected:
            raise RecipeError("PL_E_STALE solver inputs changed; solve again")
    for cached in snapshot["cached_components"]:
        if commands.components_store().load(cached["reference"], selected=cached["selected"]) != cached["document"]:
            raise RecipeError("PL_E_STALE solver components changed; solve again")
    if "measurement_probe" in snapshot:
        if _measurement_keys(snapshot["measurement_probe"], snapshot["measurement_keys"]) != snapshot["measurement_keys"]:
            raise RecipeError("PL_E_STALE solver measurement identity changed; solve again")
        current = placement.cached_measurements(snapshot["measurement_probe"], snapshot["measurement_keys"])
        if current != snapshot["request"]["probe"]:
            raise RecipeError("PL_E_STALE solver measurements changed; solve again")
    if snapshot.get("performance_receipt") is not None:
        calibration.validate(snapshot["performance_receipt"], snapshot["request"]["probe"], snapshot["request"]["inputs"],
            route=snapshot["route"], workload=snapshot["workload"], context=snapshot["request"].get("context") or {},
            recipe_sha256=hashlib.sha256(snapshot["recipe"].encode("utf-8")).hexdigest(),
            settings={"sets": snapshot["sets"], "layers": [{"role": layer["role"],
                      "sha256": hashlib.sha256(layer["text"].encode("utf-8")).hexdigest()} for layer in snapshot["layers"]]})


def _run(snapshot, backend, *, pick=None):
    import tempfile
    with tempfile.TemporaryDirectory(prefix="hl-solver-") as temporary:
        directory = Path(temporary)
        path = directory / "recipe.toml"
        path.write_bytes(snapshot["recipe"].encode("utf-8"))
        layers = session.Layers(sets=snapshot["sets"], workload=snapshot["workload"])
        for index, layer in enumerate(snapshot["layers"]):
            target = directory / (str(index) + layer["suffix"])
            target.write_bytes(layer["text"].encode("utf-8"))
            if layer["role"] == "profile":
                layers.profile = target
            else:
                layers.overrides.append(target)
        request = deepcopy(snapshot["request"])
        return placement.solve(path, route=snapshot["route"], layers=layers, backend=backend,
                               **request, measurement_keys=snapshot["measurement_keys"],
                               performance_receipt=snapshot.get("performance_receipt"),
                               **({"pick": pick} if pick is not None else {}))


def solve(cluster, ref, route, bundle, *, layers=None, backend=None):
    backend = placement._backend(cluster, backend)
    snapshot = _capture(cluster, ref, route, bundle, layers or session.Layers())
    result = _run(snapshot, backend)
    shown = rows(result.document)
    _check(snapshot)
    digest = content_cache._sha(snapshot)
    record = {"snapshot": snapshot, "result": result.document, "table": result.text}
    _save_plan(digest, record)
    return {"selection": digest, "solver": result.document, "rows": shown, "table": result.text,
            "calibration": snapshot["calibration"],
            "saved": False, "note": "Choose a row explicitly; no assist or grant is enabled by solving."}


def pick(cluster, digest, row_id, name, *, backend=None, replace=None):
    """Tierstream emits the layer after its semantic re-plan check; save those exact bytes, never synthesize quotas."""
    service.check_id(name, "layer name")
    if not content_cache._digest(digest) or not isinstance(row_id, str) or not row_id:
        raise RecipeError("PL_E_SCHEMA selection digest and row id are required")
    backend = placement._backend(cluster, backend)
    record = content_cache._read(commands.runtime_dir() / "solver-plans" / (digest + ".json"), "PL_E_STALE")
    snapshot = record["snapshot"]
    if content_cache._sha(snapshot) != digest:
        raise RecipeError("PL_E_STALE solver snapshot identity changed")
    candidates = [r for r in rows(record["result"]) if r.get("id") == row_id]
    if len(candidates) != 1 or not candidates[0]["saveable"]:
        raise RecipeError("PL_E_CAPABILITY unknown, refused or hypothetical row cannot be saved")
    with service.runtime_lock():
        _check(snapshot)
        picked = _run(snapshot, backend, pick=row_id)
        if picked.document != record["result"]:
            raise RecipeError("PL_E_STALE solver result changed; solve again")
        emitted = picked.overrides
        if not isinstance(emitted, bytes) or not emitted or len(emitted) > service.MAX_SAVE_BYTES or b"\r" in emitted:
            raise RecipeError("PL_E_SCHEMA core emitted no valid LF overrides")
        text = emitted.decode("utf-8")
        layer = reader.load_text(text, "picked overrides")
        if not isinstance(layer.get("set"), dict) or not layer["set"]:
            raise RecipeError("PL_E_SCHEMA picked overrides have no settings")
        # Solving may not enable assists, change budgets or redirect components, even with an incompatible core.
        route = next((r for r in reader.parse(snapshot["recipe"])["routes"] if r["id"] == snapshot["route"]), None)
        if route is None:
            raise RecipeError("PL_E_SCHEMA picked route is missing")
        base = "routes." + snapshot["route"]
        allowed = {base + ".max_rows", base + ".max_sequences", base + ".components.arena.units",
                   base + ".components.expert_file.units"}
        allowed.update(base + ".placement." + sid + ".max_context_tokens" for sid in route["devices"])
        for field in layer["set"]:
            if field not in allowed:
                raise RecipeError("PL_E_CONFLICT picked row may not change assists, budgets or component destinations")
        _check(snapshot)
        saved = service.save_layer(cluster, name, text=text, replace=replace, backend=backend)
        return dict(saved, selection=digest, row=row_id, replan_checked=True)
