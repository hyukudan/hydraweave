"""Typed engine settings backed by the existing, revision-pinned launch contracts.

This module only transforms configuration and renders previews. It never probes a
node, runs an engine, changes a dependency pin, or treats a preview as live state.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json

from . import config, engines, knobs, render


LABELS = {
    "--context": "Context length", "--capacity": "Context length",
    "--slots": "Concurrent requests", "--http-max-queue": "Waiting requests",
    "--http-max-connections": "Open connections",
    "--http-default-max-tokens": "Default response length",
    "--default-max-tokens": "Default response length", "--max-tokens": "Default response length",
    "--peer-timeout": "Peer timeout (seconds)",
    "mtp_ngram_draft": "Prompt lookup drafts", "dspark_max_inflight": "Draft admission",
    "dspark_ranks": "Nodes holding the drafter", "serve_sched": "Prefill scheduling",
}
FAMILY_LABELS = {"qwen-replicas": "Qwen", "ds41-pair": "DeepSeek", "minimax-pair": "MiniMax"}
CHANGE_KEYS = ("overrides", "advanced", "port", "assets", "api_key_files")


def _object(value, where, allowed):
    return config.record(value, where, optional=allowed)


def _preset_default(data, flag):
    """Only literal argv is evidence of a default; never guess a C++ default."""
    argv = data["argv"]
    if flag in argv:
        i = argv.index(flag) + 1
        if i < len(argv) and argv[i].isdecimal():
            return int(argv[i])
    return None


def controls(profile, data):
    """The form's value is an override (null = unset), distinct from its effective value."""
    port_flag = render.port_flag(data)
    overrides = profile.get("overrides", {})
    port = overrides.get(port_flag, profile.get("port"))
    result = [{"key": "port", "section": "profile", "label": "Engine port", "type": "integer",
               "min": 1, "max": 65535, "choices": [], "value": port,
               "default": data["port"], "effective": port if port is not None else data["port"],
               "source": "override" if port is not None else "preset", "editable": True,
               "description": "The engine listener. The existing authenticated router uses this port too.",
               "restart": True, "why": None}]
    for flag, rule in data["knobs"].items():
        if flag == port_flag:
            continue
        values = rule.get("values")
        default = _preset_default(data, flag)
        value = overrides.get(flag)
        fixed = rule["min"] == rule["max"] or values is not None and len(values) == 1
        why = ("This release is qualified only for " + str((values or [rule["min"]])[0]) +
               ". A larger value needs another qualified release.") if fixed else None
        result.append({"key": flag, "section": "overrides", "label": LABELS.get(flag, flag[2:].replace("-", " ").capitalize()),
                       "type": "select" if values else "integer", "choices": [{"value": v, "label": str(v)} for v in values or []],
                       "min": rule["min"], "max": rule["max"], "value": value, "default": default,
                       "effective": value if value is not None else default,
                       "source": "override" if flag in overrides else "preset" if default is not None else "engine-default",
                       "editable": not fixed or flag in overrides, "restart": True, "why": why,
                       "description": why or "An unset value keeps the pinned recipe and engine defaults."})
    table = knobs.check_table(data)
    for name, rule in table.items():
        value = profile.get("advanced", {}).get(name)
        result.append({"key": name, "section": "advanced", "label": LABELS.get(name, name.replace("_", " ").capitalize()),
                       "type": "select", "choices": [{"value": v, "label": str(v)} for v in rule["values"]],
                       "value": value, "default": rule["production"],
                       "effective": rule["production"] if value is None else value,
                       "source": "override" if name in profile.get("advanced", {}) else "preset",
                       "editable": True, "restart": True, "why": None,
                       "description": rule.get("effect", "Qualified settings for this engine revision."),
                       "target": rule["target"], "qualification": deepcopy(rule["qualification"]),
                       "requires": deepcopy(rule.get("requires", {})), "conflicts": deepcopy(rule.get("conflicts", {}))})
    return result


def preset_document(name):
    data = config.preset(name)
    engine = engines.release_adapter(name)
    return {"id": name, "label": FAMILY_LABELS.get(engine.family, engine.family) + " · " + name,
            "family": engine.family, "topology": engine.topology, "engine_sha": data["engine_sha"],
            "qualified": bool(data.get("qualified")), "receipt": data["receipt"],
            "required_assets": list(data["assets"]), "port": data["port"],
            "controls": controls({}, data), "requires_api_key": engine.key_file,
            "bind": engine.bind, "notes": data.get("receipt_note"),
            "components": engines.components(data)}


def asset_rows(cluster, name):
    p = cluster.profiles[name]
    kind = p.get("preset", name)
    data = config.preset(kind)
    return [{"machine": mid, "key": key, "required": True,
             "configured": key in cluster.assets.get(mid, {}),
             "value": deepcopy(cluster.assets.get(mid, {}).get(key)),
             "verification": "declared" if key in cluster.assets.get(mid, {}) else "missing"}
            for mid in engines.members(p, config.family(kind)) for key in data["assets"]]


def preview(cluster, name):
    """Use the production renderer unchanged. Missing assets/releases are honest setup tasks."""
    p = cluster.profiles[name]
    kind = p.get("preset", name)
    engine = engines.release_adapter(kind)
    mids = engines.members(p, engine.family)
    managed = {
        "release_id": "The installed release is selected and verified during launch.",
        "api_key_files": "Node API-key files are generated and provisioned during launch.",
        "replica_api_key_files": "Router copies of the node keys are provisioned during launch.",
        "api_key_file": "The router's API-key file is provisioned during launch.",
        "prefill_key_file": "The prefill service's router key is provisioned during launch.",
    }
    generated = []
    if "release_id" not in p:
        generated.append("release_id")
    if engine.key_file:
        if any(mid not in p.get("api_key_files", {}) for mid in mids):
            generated.append("api_key_files")
        if any(mid not in cluster.router.get("replica_api_key_files", {})
               for mid in engines.router_members(engine.family, mids)):
            generated.append("replica_api_key_files")
    if "api_key_file" not in cluster.router:
        generated.append("api_key_file")
    errors, deferred, processes, router = [], [], [], None
    missing = {}
    for row in asset_rows(cluster, name):
        if not row["configured"]:
            missing.setdefault(row["machine"], []).append(row["key"])
    for mid, roles in missing.items():
        errors.append({"part": "assets", "message": mid + ": missing asset records " + ", ".join(roles)})

    def failure(part, exc):
        message = str(exc)
        resource = next((key for needle, key in (
            ("profile.release_id: required", "release_id"),
            ("missing node-local api_key_files path", "api_key_files"),
            ("missing router-host-local replica_api_key_files path", "replica_api_key_files"),
            ("non-loopback router requires a router-host-local api_key_file", "api_key_file"),
            ("missing router-host-local prefill service key file", "prefill_key_file"),
        ) if needle in message), None)
        if resource is not None:
            if resource not in generated:
                generated.append(resource)
            deferred.append({"part": part, "resource": resource, "message": managed[resource]})
        elif not (missing and ": missing assets " in message):
            errors.append({"part": part, "message": message})

    try:
        processes = [asdict(p) for p in render.render_processes(cluster, name)]
    except (config.ConfigError, OSError, ValueError) as exc:
        failure("processes", exc)
    try:
        router = render.render_router(cluster, name)
    except (config.ConfigError, OSError, ValueError) as exc:
        failure("router", exc)
    return {"rendered": not errors and not deferred, "processes": processes, "router": router,
            "errors": errors, "error": "; ".join(e["message"] for e in errors) or None,
            "pending_setup": deepcopy(errors), "deferred": deferred,
            "generated_at_launch": [{"resource": key, "message": managed[key]} for key in generated],
            "source": "pinned-recipe-render", "runtime_applied": False,
            "note": "Launch preview, not an observation of a running engine. Installation and startup recheck the release and assets."}


def profile_document(cluster, name):
    p = cluster.profiles[name]
    kind = p.get("preset", name)
    data = config.preset(kind)
    engine = engines.release_adapter(kind)
    assets = asset_rows(cluster, name)
    return {"id": name, "preset": kind, "family": engine.family, "topology": engine.topology,
            "nodes": engines.members(p, engine.family), "configuration": deepcopy(p),
            "controls": controls(p, data), "assets": assets,
            "missing_assets": sum(not row["configured"] for row in assets),
            "preview": preview(cluster, name), "qualification": render.launch_summary(cluster, name),
            "capabilities": {"declared": list(engine.capabilities), "source": "pinned-adapter",
                             "runtime": None,
                             "why": "No verified runtime capability document is available here. Only controls in this pinned recipe are editable."},
            "engine_sha": data["engine_sha"], "restart_required": True,
            "unsupported": ["Arbitrary engine flags", "Live numerical changes", "Unqualified context expansion"],
            "notes": ["Auto removes the override and restores the pinned recipe; it does not change the recipe's fixed settings.",
                      "Engine, Trellis, driver and model revisions remain pinned."]}


def _merge_assets(raw, p, kind, changes):
    if not isinstance(changes, dict):
        raise config.ConfigError("assets: expected an object keyed by machine")
    mids = engines.members(p, config.family(kind))
    _object(changes, "assets", mids)
    allowed = config.preset(kind)["assets"]
    for mid, entries in changes.items():
        _object(entries, "assets." + mid, allowed)
        for key, value in entries.items():
            if value is None:
                raw["assets"].get(mid, {}).pop(key, None)
            else:
                # config.parse validates all paths, byte counts and full SHA-256s.
                raw["assets"].setdefault(mid, {})[key] = deepcopy(value)
        if not raw["assets"].get(mid):
            raw["assets"].pop(mid, None)


def apply_changes(raw, name, changes):
    """Patch only allowlisted fields; None removes a chosen override. Preserve unrelated config."""
    config.identifier(name, "profile")
    _object(changes, "changes", CHANGE_KEYS)
    if name not in raw["profiles"]:
        raise LookupError("unknown profile " + name)
    new = deepcopy(raw)
    p = new["profiles"][name]
    kind = p.get("preset", name)
    data = config.preset(kind)
    for section, allowed in (("overrides", data["knobs"]), ("advanced", data.get("advanced", {}))):
        if section not in changes:
            continue
        chosen = _object(changes[section], "changes." + section, allowed)
        target = p.setdefault(section, {})
        for key, value in chosen.items():
            if value is None:
                target.pop(key, None)
            else:
                target[key] = value
        if not target:
            p.pop(section, None)
    port_flag = render.port_flag(data)
    if "port" in changes:
        if port_flag in changes.get("overrides", {}):
            raise config.ConfigError("set the engine port once, using changes.port")
        # The dedicated port setting replaces legacy --http-port/--port overrides.
        p.get("overrides", {}).pop(port_flag, None)
        if not p.get("overrides"):
            p.pop("overrides", None)
        if changes["port"] is None:
            p.pop("port", None)
        else:
            p["port"] = changes["port"]
    if "api_key_files" in changes:
        mids = engines.members(p, config.family(kind))
        _object(changes["api_key_files"], "api_key_files", mids if engines.release_adapter(kind).key_file else ())
        keys = p.setdefault("api_key_files", {})
        for mid, value in changes["api_key_files"].items():
            if value is None:
                keys.pop(mid, None)
            else:
                keys[mid] = value
        if not keys:
            p.pop("api_key_files", None)
    if "assets" in changes:
        _merge_assets(new, p, kind, changes["assets"])
    cluster = config.parse(new)
    if len((json.dumps(new, indent=2, ensure_ascii=False) + "\n").encode("utf-8")) > config.MAX_CONFIG_BYTES:
        raise config.ConfigError("configuration would exceed 256 KiB")
    return new, cluster


def create_profile(raw, arguments):
    allowed = ("id", "preset", "nodes", "rank0", "rank1", "port", "assets", "api_key_files", "changes")
    _object(arguments, "profile", allowed)
    name = config.identifier(arguments.get("id"), "profile.id")
    kind = arguments.get("preset")
    config.preset(kind)
    if name in raw["profiles"]:
        raise config.ConfigError("a profile with this id already exists")
    engine = engines.release_adapter(kind)
    if engine.topology == "replicas":
        if "rank0" in arguments or "rank1" in arguments:
            raise config.ConfigError("replica profiles use nodes, not rank0/rank1")
        p = {"preset": kind, "nodes": deepcopy(arguments.get("nodes"))}
    else:
        if "nodes" in arguments:
            raise config.ConfigError("pair profiles require rank0 and rank1")
        p = {"preset": kind, "rank0": arguments.get("rank0"), "rank1": arguments.get("rank1")}
    new = deepcopy(raw)
    new["profiles"][name] = p
    cluster = config.parse(new)  # validate membership before using the machine keys
    if any(cluster.machines[mid].access not in {"ssh", "agent"} for mid in engines.members(p, engine.family)):
        raise config.ConfigError("engine workers require Linux SSH or paired agent access")
    changes = deepcopy(arguments.get("changes", {}))
    _object(changes, "changes", CHANGE_KEYS)
    for key in ("port", "assets", "api_key_files"):
        if key in arguments:
            if key in changes:
                raise config.ConfigError("duplicate field " + key)
            changes[key] = arguments[key]
    return apply_changes(new, name, changes)


def diff(before, after, name):
    """Small review document with real old/new values, including shared asset edits."""
    rows = []
    old, new = before.get("profiles", {}).get(name), after["profiles"][name]
    if old is None:
        rows.append({"path": "profiles." + name, "before": None, "after": deepcopy(new)})
    else:
        for key in sorted(set(old) | set(new)):
            if old.get(key) != new.get(key):
                rows.append({"path": "profiles." + name + "." + key,
                             "before": deepcopy(old.get(key)), "after": deepcopy(new.get(key))})
    for mid in sorted(set(before["assets"]) | set(after["assets"])):
        for key in sorted(set(before["assets"].get(mid, {})) | set(after["assets"].get(mid, {}))):
            old_asset, new_asset = before["assets"].get(mid, {}).get(key), after["assets"].get(mid, {}).get(key)
            if old_asset != new_asset:
                rows.append({"path": "assets." + mid + "." + key, "before": deepcopy(old_asset), "after": deepcopy(new_asset)})
    return rows
