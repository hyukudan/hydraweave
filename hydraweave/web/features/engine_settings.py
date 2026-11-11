"""Engine settings: read-only previews and locked, revision-checked configuration edits."""
from contextlib import ExitStack
from pathlib import Path
import re

from hydraweave import config, engine_settings as settings, engines, procs
from hydraweave.recipes import service
from . import ops


def _request(arguments, creating=False):
    if creating or arguments.get("op") == "create":
        ops.strict(arguments, ("version", "op", "id", "profile", "preset", "nodes", "rank0", "rank1", "port", "assets", "api_key_files", "changes"))
        if "op" in arguments and arguments["op"] != "create":
            raise ValueError("op must be create")
        if "id" in arguments and "profile" in arguments and arguments["id"] != arguments["profile"]:
            raise ValueError("profile and id disagree")
        name = arguments.get("id", arguments.get("profile"))
        config.identifier(name, "profile")
        new = {k: v for k, v in arguments.items() if k not in ("version", "op", "profile")}
        new["id"] = name
        return name, True, new
    ops.strict(arguments, ("version", "op", "profile", "changes"), ("profile", "changes"))
    if arguments.get("op", "update") != "update":
        raise ValueError("op must be create or update")
    name = config.identifier(arguments["profile"], "profile")
    return name, False, arguments["changes"]


def _apply(raw, request):
    name, creating, payload = request
    return settings.create_profile(raw, payload) if creating else settings.apply_changes(raw, name, payload)


def _cached_blocker(api, nodes):
    job = ops.core_job_running(api)
    if job:
        return "another job is running (" + job + ")"
    cached = api.cached_snapshot()
    if not isinstance(cached, dict):
        return None
    for row in (cached.get("profiles") or {}).get("profiles") or []:
        members = set(row.get("nodes") or [])
        if isinstance(row.get("router"), str):
            members.add(row["router"])
        if set(nodes) & members and row.get("state") in ops.RUNNING_PROFILE_STATES:
            return "Stop " + row.get("id", "the active profile") + " before editing these settings."
        for lease in row.get("leases") or []:
            if lease.get("machine") in nodes and lease.get("state") not in ops.FINAL_LEASE_STATES:
                return "A live lease holds " + lease["machine"] + "; stop its profile first."
    return None


def document(api, query):
    ops.strict(query, ("profile",))
    raw, version = ops._read_config(api)
    cluster = config.parse(raw)
    selected = query.get("profile")
    if selected is not None:
        config.identifier(selected, "profile")
    if selected is not None and selected not in cluster.profiles:
        raise LookupError("unknown profile " + str(selected)[:64])
    profiles = []
    for name in cluster.profiles:
        if selected is not None and name != selected:
            continue
        row = settings.profile_document(cluster, name)
        row["blocked"] = _cached_blocker(api, row["nodes"])
        row["editable"] = row["blocked"] is None
        row["version"] = version
        profiles.append(row)
    return api.document("engine-settings", version=version, profiles=profiles,
                        presets=[settings.preset_document(name) for name in config.PRESET_NAMES],
                        machines=[{"id": m.id, "os": m.os, "access": m.access, "selectable": m.os == "linux" and m.access == "ssh"}
                                  for m in cluster.machines.values()],
                        writes_apply="next-launch", runtime_applied=False,
                        note="Settings are checked against the pinned release. Saving rechecks active launches and node leases.")


def _validated(api, raw, version, request):
    name, creating, _ = request
    new, cluster = _apply(raw, request)
    p = settings.profile_document(cluster, name)
    changed = settings.diff(raw, new, name)
    return new, cluster, {"valid": True, "error": None, "version": version, "profile": name,
                          "creating": creating, "configuration": cluster.profiles[name],
                          "preview": p["preview"], "controls": p["controls"], "assets": p["assets"],
                          "qualification": p["qualification"], "changes": changed,
                          "changed": bool(changed), "restart_required": bool(changed),
                          "runtime_applied": False, "blocked": _cached_blocker(api, p["nodes"])}


def validate(api, arguments):
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    request = _request(arguments)
    raw, version = ops._read_config(api)
    if arguments.get("version") is not None and arguments["version"] != version:
        return {"valid": False, "error": "The configuration changed. Reload and review before saving.",
                "version": version, "stale": True}
    try:
        _, _, review = _validated(api, raw, version, request)
        return review
    except (ValueError, LookupError, PermissionError) as exc:
        return {"valid": False, "error": str(exc).strip("'\""), "version": version}


def _affected(raw, new, name, creating):
    cluster = config.parse(new)
    p = cluster.profiles[name]
    nodes = set(engines.members(p, config.family(p.get("preset", name)))) if not creating else set()
    for mid in set(raw["assets"]) | set(new["assets"]):
        if raw["assets"].get(mid) != new["assets"].get(mid):
            nodes.add(mid)
    # Global assets can be shared by several configured profiles.
    profiles = {name} | {pname for mid in nodes for pname in ops.profiles_using(cluster, mid)}
    return nodes, profiles


def _runtime_blocker(api, cluster, mid):
    reason, unknown = ops.occupancy(api, cluster, mid)
    if reason and unknown and "node's lease records are unreadable" in reason:
        # A first-run machine can be configured before its node tool is installed,
        # but an unreachable machine is not evidence that the tool is absent.
        machine = cluster.machines[mid]
        if machine.access == "ssh":
            try:
                if api.orchestrate._absent(api.orchestrate._client(machine)) is True:
                    return None
            except Exception:
                pass
    return reason


def _save(api, arguments, creating=False):
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    request = _request(arguments, creating)
    if not isinstance(arguments.get("version"), str) or not re.fullmatch(r"[0-9a-f]{64}", arguments["version"]):
        raise ValueError("version is required: reload the configuration and review it")
    name, creating, _ = request
    job = ops.core_job_running(api)
    if job:
        raise PermissionError("another job is running (" + job + ")")
    hold = None
    with ops._write_lock, ops.ConfigLock(api.config_path), ExitStack() as locks:
        locks.enter_context(service.configured(api.config_path))
        raw, version = ops._read_config(api)
        if version != arguments["version"]:
            raise PermissionError("the configuration changed since it was read; reload and review")
        new, cluster, review = _validated(api, raw, version, request)
        if not review["changed"]:
            return {**review, "saved": True, "note": "No settings changed."}
        nodes, profiles = _affected(raw, new, name, creating)
        try:
            # Same per-profile locks as up/down protect the lease hand-over. The
            # shared configuration lock also serializes the machine editor.
            for profile in sorted(profiles):
                path = api.orchestrate._state_path(profile)
                if isinstance(path, (str, Path)):
                    lock = procs.InstanceLock(Path(path).with_suffix(".lock"))
                    try:
                        lock.acquire()
                    except procs.ProcessError as exc:
                        raise PermissionError("profile " + profile + " is being started or stopped; retry when idle") from exc
                    locks.callback(lock.close)
                    if Path(path).exists():
                        raise PermissionError("profile " + profile + " has saved running state; stop it before editing")
            hold = api._admission().acquire("core", nodes or {"config"}, label="engine settings " + name)
            for mid in sorted(nodes):
                reason = _runtime_blocker(api, cluster, mid)
                if reason:
                    raise PermissionError(mid + ": " + reason + "; settings were not changed")
            # Another tab may have admitted a start during the node reads. Commit
            # under the same lock the API uses to reserve its mutating job slot.
            with api._job_lock:
                active = api._active
                if active and active in api._jobs:
                    raise PermissionError("another job is running (" + api._jobs[active]["action"] + ")")
                _, last_version = ops._read_config(api)
                if last_version != version:
                    raise PermissionError("the configuration changed since it was read; reload and review")
                new_version = ops.write_config(api.config_path, new)
            # Force the next ordinary portal poll to re-read the saved settings.
            with api._poll_lock:
                api._expires = 0
        finally:
            if hold:
                api._admission().release(hold)
    return {**review, "saved": True, "version": new_version, "blocked": None,
            "note": "Saved for the next launch. Running processes and dependency revisions were not changed."}


def save(api, arguments):
    return _save(api, arguments)


def create(api, arguments):
    return _save(api, arguments, creating=True)


def register(reg):
    reg.get("engine-settings", document)
    reg.post("engine-settings/validate", validate)
    reg.post("engine-settings/save", save, audited=True)
    reg.post("engine-settings/create", create, audited=True)
    reg.capability("engine-settings", True, "Typed controls and previews for revision-pinned engine profiles")
