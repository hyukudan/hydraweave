"""First-run configuration and explicit controller/router placement.

GET is a passive summary of configuration and cached observations. Initializing a
configuration never probes hardware, invents model identities or starts workers.
Router edits apply to the next launch and share the launcher's profile locks.
"""

from contextlib import ExitStack
from copy import deepcopy
import os
from pathlib import Path
import socket

from hydraweave import config, engines, nodegate, orchestrate, procs
from hydraweave.recipes import service
from . import ops


def _configuration(api):
    try:
        raw, version = ops._read_config(api)
    except FileNotFoundError:
        return None, None, "missing", None
    except (OSError, ValueError) as exc:
        return None, None, "invalid", str(exc)
    try:
        cluster = config.parse(raw)
    except (ValueError, OSError) as exc:
        return None, version, "invalid", str(exc)
    return cluster, version, "valid", None


def _router(cluster):
    settings = cluster.router if cluster else {}
    machine = settings.get("machine", "local")
    endpoint = orchestrate.router_listen(cluster, machine) if cluster else "127.0.0.1:18600"
    return {"machine": machine, "listen": endpoint,
            "placement": "controller" if machine == "local" else "node",
            "restart_required": True, "state": "configured", "active": None,
            "note": "Placement applies at the next launch. Runtime status is shown in Engines."}


def _requirements(cluster):
    rows = []
    if cluster is None:
        return rows
    for name, profile in cluster.profiles.items():
        preset = profile.get("preset", name)
        data = config.preset(preset)
        adapter = engines.release_adapter(preset)
        nodes = profile.get("nodes", [profile.get("rank0"), profile.get("rank1")])
        missing = [{"machine": mid, "asset": asset}
                   for mid in nodes for asset in data["assets"]
                   if asset not in cluster.assets.get(mid, {})]
        rows.append({"id": name, "preset": preset, "topology": adapter.topology,
                     "machines": nodes, "missing_assets": missing,
                     "asset_state": "missing" if missing else "declared",
                     "persistent_service": "service-unit" in adapter.capabilities and
                     all(not cluster.machines[mid].lab_mode for mid in nodes),
                     "state": "configured", "installed": None})
    return rows


def _os_name():
    """The controller's OS name without platform.system(), which runs a `ver` child process on Windows."""
    return "Windows" if os.name == "nt" else os.uname().sysname


def overview(api, query):
    ops.strict(query, ())
    cluster, version, state, error = _configuration(api)
    router = _router(cluster)
    profiles = _requirements(cluster)
    machines = cluster.machines if cluster else {}
    local = next((m for m in machines.values() if m.access == "local"), None)
    pinned = ops._pinned(api, cluster)
    cached = api.cached_snapshot() or {}
    observed = {row["id"]: row for row in (cached.get("devices", {}).get("machines") or [])}
    nodes = []
    for mid, machine in machines.items():
        roles = []
        if machine.access == "local":
            roles.append("controller")
        if router["machine"] == mid or (machine.access == "local" and router["machine"] == "local"):
            roles.append("router")
        for name, profile in (cluster.profiles if cluster else {}).items():
            if mid in profile.get("nodes", []):
                roles.append("replica:" + name)
            for key in ("rank0", "rank1"):
                if profile.get(key) == mid:
                    roles.append(key + ":" + name)
        evidence = observed.get(mid, {})
        nodes.append({"id": mid, "os": machine.os, "access": machine.access,
                      "address": machine.http_address, "roles": roles,
                      "trust": "local" if machine.access == "local" else
                      "pinned" if pinned.get(mid) else "unverified",
                      "state": evidence.get("health"), "lab_mode": machine.lab_mode,
                      "last_seen": evidence.get("last_seen"),
                      "configured": True, "observed": evidence.get("last_seen") is not None,
                      "note": "Configure this host as an SSH worker as well if it will run inference."
                      if machine.access == "local" else None})
    workers = [row for row in nodes if row["access"] == "ssh" and row["os"] == "linux"]
    trustworthy = bool(workers) and all(row["trust"] == "pinned" for row in workers)
    assets_declared = bool(profiles) and all(not row["missing_assets"] for row in profiles)
    any_running = any(row.get("state") == "running" for row in
                      (cached.get("profiles", {}).get("profiles") or []))
    steps = [
        {"id": "configuration", "title": "Create your workspace", "done": state == "valid",
         "detail": error or ("The configuration stays on this controller." if cluster else
                              "Create a local configuration, then add your machines."),
         "href": "#home", "action": "setup/initialize" if state == "missing" else None},
        {"id": "nodes", "title": "Add your Linux nodes", "done": bool(workers),
         "detail": "Add SSH addresses and the installation folder on each machine.", "href": "#ops"},
        {"id": "trust", "title": "Verify the node identities", "done": trustworthy,
         "detail": "Compare and pin SSH fingerprints, then test each connection.", "href": "#ops"},
        {"id": "profile", "title": "Choose a model and its nodes", "done": bool(profiles),
         "detail": "Create a profile from a supported preset in Engine settings.", "href": "#engine-settings"},
        {"id": "assets", "title": "Assign model files", "done": assets_declared,
         "detail": "Paths, sizes and hashes describe the files on each node; startup verifies them.",
         "href": "#engine-settings"},
        {"id": "components", "title": "Prepare the node tools and engine", "done": any_running,
         "detail": "Update node tools, review the pinned installation plan and build on the selected node.",
         "href": "#components"},
        {"id": "launch", "title": "Start and check the model", "done": any_running,
         "detail": "Readiness is established by the running engine, not by saving configuration.", "href": "#engines"},
    ]
    found_next = False
    for step in steps:
        done = step.pop("done")
        step["state"] = "done" if done else "next" if not found_next else "pending"
        if state == "invalid" and step["id"] == "configuration":
            step["state"] = "blocked"
        if not done:
            found_next = True
    next_action = next((row for row in steps if row["state"] != "done"), None)
    lab_nodes = [row["id"] for row in workers if row["lab_mode"]]
    return api.document("setup", configured=state == "valid",
                        configuration={"state": state, "path": str(api.config_path),
                                       "version": version, "error": error},
                        controller={"hostname": socket.gethostname(), "os": _os_name(),
                                    "machine_id": local.id if local else None,
                                    "portal_bind": "127.0.0.1", "role": "control-plane"},
                        router=router, nodes=nodes, profiles=profiles, checklist=steps,
                        next_action=next_action,
                        lifecycle={"mode": "bounded-leases" if lab_nodes else "profile-dependent",
                                   "lab_nodes": lab_nodes,
                                   "note": "Closing a browser tab is not a stop command. Node supervisors own "
                                           "running engines; lab leases still expire at their deadline. A local "
                                           "router needs its controller online. Persistent Qwen service mode "
                                           "is separate; paired engines remain lease-based."},
                        recommendation={"title": "One controller, explicit node roles",
                                        "detail": "Use a Linux machine that stays on as the controller for a "
                                                  "dedicated cluster, and open its loopback portal through SSH. "
                                                  "A Windows workstation also works as the controller. Choose "
                                                  "router placement per supported profile; rank 0 only identifies "
                                                  "the first inference worker, not the controller."})


def initialize(api, arguments):
    ops.strict(arguments, ())
    path = Path(api.config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with ops._write_lock, ops.ConfigLock(path):
        if path.exists() or path.is_symlink():
            raise PermissionError("a configuration already exists; open it instead of initializing again")
        machine_os = "windows" if os.name == "nt" else "linux"
        root = Path.home() / ".local" / "share" / "hydraweave" / "node"
        raw = {"schema": "hydraweave/cluster-v1", "machines": [
            {"id": "controller", "os": machine_os, "access": "local", "http_address": "127.0.0.1",
             "install_root": str(root), "lab_mode": True, "devices": []}],
            "assets": {}, "profiles": {},
            "router": {"machine": "local", "listen": "127.0.0.1:18600"},
            "pc": {"ui_listen": "127.0.0.1:18700"}}
        config.parse(raw)
        version = ops.write_config(path, raw)
    nodegate.prepare(path, ["controller"])
    api._expires = 0
    return {"initialized": True, "version": version, "machines": ["controller"],
            "note": "Workspace created. Add a Linux SSH worker; no hardware was probed or started."}


def _router_edit(api, arguments, raw=None):
    ops.strict(arguments, ("version", "machine", "port"), ("machine",))
    if raw is None:
        raw, _ = ops._read_config(api)
    cluster = config.parse(raw)
    selected = arguments["machine"]
    config.identifier(selected, "router.machine")
    if selected != "local":
        machine = cluster.machines.get(selected)
        if machine is None or machine.os != "linux" or machine.access != "ssh":
            raise ValueError("choose the controller (local) or a configured Linux SSH node")
        host = machine.http_address
    else:
        host = "127.0.0.1"
    previous = _router(cluster)
    port = arguments.get("port", config.listen(previous["listen"], "router.listen")["port"])
    config.integer(port, "router.port", 1, 65535)
    failures = []
    for name, profile in cluster.profiles.items():
        adapter = engines.release_adapter(profile.get("preset", name))
        if adapter.bind == "loopback" and selected != profile.get("rank0"):
            failures.append(name + " requires its router on rank 0 (" + str(profile.get("rank0")) + ")")
        if profile.get("prefill_service") and selected != "local":
            failures.append(name + " uses a prefill service on the controller and requires a local router")
    if failures:
        raise ValueError("; ".join(failures))
    changed = deepcopy(raw)
    changed.setdefault("router", {}).update(machine=selected, listen=(f"[{host}]:{port}" if ":" in host else f"{host}:{port}"))
    # Runtime launch creates host-local keys. Old paths must not silently move to another host.
    if previous["machine"] != selected:
        changed["router"].pop("api_key_file", None)
        changed["router"].pop("replica_api_key_files", None)
    config.parse(changed)
    return changed, {"machine": selected, "listen": changed["router"]["listen"],
                     "placement": "controller" if selected == "local" else "node",
                     "restart_required": True, "applies": "next launch", "starts_processes": False,
                     "note": "Recipe launches keep their explicit placement and may require the controller."}


def router_validate(api, arguments):
    try:
        _, preview = _router_edit(api, arguments)
        return {"valid": True, "error": None, "preview": preview}
    except (OSError, ValueError, LookupError) as exc:
        return {"valid": False, "error": str(exc), "preview": None}


def router_save(api, arguments):
    if not isinstance(arguments, dict) or not isinstance(arguments.get("version"), str):
        raise ValueError("version is required: reload and review the configuration")
    job = ops.core_job_running(api)
    if job:
        raise PermissionError("another job is running (" + job + ")")
    with service.configured(api.config_path), ops._write_lock, ops.ConfigLock(api.config_path), ExitStack() as locks:
        raw, version = ops._read_config(api)
        if version != arguments["version"]:
            raise PermissionError("the configuration changed since it was read; reload and review")
        new_raw, preview = _router_edit(api, arguments, raw)
        # up() holds these same locks until its launch record is complete. The
        # config lock prevents a new profile being added while the set is locked.
        for name in sorted(raw["profiles"]):
            path = orchestrate._state_path(name)
            try:
                lock = procs.InstanceLock(path.with_suffix(".lock")).acquire()
            except procs.ProcessError as exc:
                raise PermissionError("a launch is changing " + name + "; retry after it finishes") from exc
            locks.callback(lock.close)
            if path.exists() or path.is_symlink():
                raise PermissionError("stop profile " + name + " before changing router placement")
        with api._job_lock:
            active = api._active
            if active and active in api._jobs:
                raise PermissionError("another job is running (" + api._jobs[active]["action"] + ")")
            version = ops.write_config(api.config_path, new_raw)
    api._expires = 0
    return {"saved": True, "version": version, "preview": preview,
            "note": "Router placement saved for the next launch. No process was started or moved."}


def register(reg):
    reg.get("setup", overview)
    reg.post("setup/initialize", initialize, audited=True)
    reg.post("setup/router/validate", router_validate)
    reg.post("setup/router/save", router_save, audited=True)
    reg.capability("setup", True, "Workspace creation and explicit controller/router placement")
