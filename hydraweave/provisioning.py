"""Desired node contents, using the existing engine recipes, bundles and download jobs."""
from __future__ import annotations

from copy import deepcopy
from pathlib import PurePosixPath
import time

from . import config, downloads, engines, orchestrate
from .onboarding import Client


def plan(cluster, mid, profile, inventory=None):
    settings, kind, mids = orchestrate._profile(cluster, profile)
    if mid not in mids:
        raise ValueError("node does not belong to this profile")
    machine = cluster.machines[mid]
    data = config.preset(orchestrate._recipe_preset(kind))
    adapter = engines.adapter(kind)
    versions = engines.lock()
    options = engines.recipe_options(data, kind)
    recipe = {"configure": options.pop("configure", versions["toolchains"]["hip_configure"]),
              "targets": list(adapter.targets), "trellis": adapter.trellis is not None,
              "ctest_regex": "^strix-(serve-(frontend|deepseek-chat|api)|host-upload)-tests$",
              "required_flags": orchestrate._required_flags(kind, list(adapter.targets)), **options}
    pins = engines.components(data, versions)
    blockers = [f"{name}: unpublished pin requires a local source bundle (--source {name}=DIR@COMMIT)"
                for name, row in pins.items() if not row.get("commit")]
    base = {"root": machine.install_root, "lab_dir": orchestrate._lab(cluster, mid)}
    models, manifests = [], deepcopy(cluster.assets.get(mid, {}))
    for asset, source in downloads.sources(cluster, profile).items():
        existing = manifests.get(asset)
        if existing:
            models.append({"asset": asset, "existing": existing, "source": source})
            continue
        if source["status"] != "download" or len(source.get("files", [])) != 1 or source.get("after"):
            blockers.append(f"{asset}: {source.get('why', 'requires a prepared model/conversion; supply its manifest')}")
            continue
        item = source["files"][0]
        directory = PurePosixPath(machine.install_root).parent / "hydraweave-models" / kind / asset
        path = str(directory / PurePosixPath(item["file"]).name)
        manifest = {"path": path, "bytes": item["bytes"], "sha256": item["sha256"]}
        candidates = []
        for root_row in (inventory or {}).get("roots", []):
            if root_row.get("path") not in config.model_roots_of(machine):
                continue
            for unit in root_row.get("units", []):
                if (unit.get("kind") == "file" and unit.get("name") == PurePosixPath(item["file"]).name
                        and unit.get("bytes") == item["bytes"]):
                    candidates.append(str(PurePosixPath(root_row["path"]) / unit["name"]))
        if len(candidates) == 1:
            manifest["path"] = candidates[0]
        manifests[asset] = manifest
        models.append({"asset": asset, "existing": manifest if len(candidates) == 1 else None,
                       "manifest": manifest, "source": source})
    return {"machine": mid, "profile": profile, "engine": dict(base, components=pins, recipe=recipe),
            "models": models, "manifests": manifests, "model_roots": config.model_roots_of(machine),
            "blockers": blockers, "starts_inference": False}


def _wait(client, root, job, report, cancelled):
    last = None
    while True:
        if cancelled():
            # Existing download cancellation retains partials. Builds have bounded existing job contracts.
            try:
                client.call("download-cancel", {"root": root, "job": job})
            finally:
                raise ValueError("provisioning cancelled")
        state = client.call("job", {"root": root, "job": job})
        progress = state.get("progress") or {}
        message = (f"{progress['file']}: {progress['bytes']}/{progress['total_bytes']} bytes"
                   if progress else (state.get("tail") or state.get("state", "working")))
        if message != last:
            report(message[-4096:])
            last = message
        if state.get("state") == "succeeded":
            return state
        if state.get("state") in {"failed", "cancelled", "aborted"}:
            raise ValueError(state.get("error") or state.get("tail") or "node provisioning failed")
        time.sleep(0.25)


def execute(cluster, path, mid, profile, *, report=lambda text: None, cancelled=lambda: False, sources=None):
    from . import nodegate
    with nodegate.hold(path, mid, wait=1):
        return _execute(cluster, path, mid, profile, report=report, cancelled=cancelled, sources=sources)


def _execute(cluster, path, mid, profile, *, report, cancelled, sources):
    """Build/download only. No inference binary runs here; GPU work still uses the existing lab leases/queue."""
    from .web.features import ops
    desired = plan(cluster, mid, profile)
    machine = cluster.machines[mid]
    client = Client(machine, path)
    root = machine.install_root
    report(f"Preparing {mid}: engine and {len(desired['models'])} model assets")
    # Keep model discovery in the shared library. The node scans only its configured lab roots.
    roots = desired["model_roots"]
    inventory = client.call("models", {"root": root, "lab_dir": desired["engine"]["lab_dir"],
                                       "lab_required": machine.lab_mode, "roots": roots}) if roots else {}
    report("Existing model roots inspected: " + ", ".join(roots))
    if inventory:
        desired = plan(cluster, mid, profile, inventory)
    remaining = [text for text in desired["blockers"] if "unpublished pin" not in text]
    if remaining:
        raise ValueError("; ".join(remaining))
    client.call("provision-prepare", {"root": root, "directories": []})
    # Missing/unpublished sources are resolved and verified by the same Git bundle pipeline as CLI install.
    data = config.preset(orchestrate._recipe_preset(cluster.profiles[profile].get("preset", profile)))
    staged = orchestrate._stage_bundles(cluster, client, mid, data, engines.lock(), sources or {})
    payload = desired["engine"]
    payload["components"] = orchestrate._components(data, engines.lock(), sources or {}, staged)
    payload["asset_paths"] = [row["path"] for row in desired["manifests"].values()]
    # The agent prepares only dedicated download subdirectories and keeps existing lab models read-only.
    for row in desired["models"]:
        source = row["source"]
        manifest = row.get("existing") or row["manifest"]
        present = client.call("stat-assets", {"paths": [manifest["path"]]})
        facts = present.get("assets", {}).get(manifest["path"], {})
        if facts.get("bytes") == manifest["bytes"]:
            verified = client.call("verify-model", {"root": root, "lab_dir": payload["lab_dir"],
                                                     **{k: manifest[k] for k in ("path", "bytes", "sha256")}})
            _wait(client, root, verified["job"], report, cancelled)
            report(row["asset"] + ": using existing model")
            continue
        if source["status"] != "download" or source.get("after"):
            raise ValueError(row["asset"] + ": no automatic source; prepare the registered model first")
        directory = str(PurePosixPath(manifest["path"]).parent)
        client.call("provision-prepare", {"root": root, "directories": [directory]})
        files = [{**item, "repo": source["repo"], "revision": source["revision"], "directory": directory,
                  "destination": str(PurePosixPath(directory) / PurePosixPath(item["file"]).name)}
                 for item in source["files"]]
        request = {"root": root, "lab_dir": payload["lab_dir"], "files": files}
        check = client.call("download-plan", request)
        if not all(row["ok"] for row in check["disk"]):
            raise ValueError("insufficient disk space for model downloads")
        report(row["asset"] + ": downloading (resuming existing partials)")
        job = client.call("download", dict(request, confirm=check["confirm"]))
        _wait(client, root, job["job"], report, cancelled)
    existing = client.call("releases", {"root": root}).get("releases", [])
    release_id = None
    for row in existing:
        if (row.get("manifest", {}).get("components") == payload["components"]
                and row.get("manifest", {}).get("recipe") == payload["recipe"]):
            release_id = row.get("release_id")
            break
    if not release_id:
        client.call("provision-prepare", {"root": root, "directories": []})
        report("Building pinned engine release (host tests only)")
        job = client.call("install", payload)
        release_id = _wait(client, root, job["job"], report, cancelled)["release_id"]
    client.call("activate", {"root": root, "profile": profile, "release_id": release_id})
    # Save only this machine's newly generated asset entries, under the existing controller write lock.
    with ops._write_lock, ops.ConfigLock(path):
        raw = config.read_json(path)
        entries = raw.setdefault("assets", {}).setdefault(mid, {})
        for name, row in desired["manifests"].items():
            entries.setdefault(name, row)
        config.parse(raw)
        ops.write_config(path, raw)
    report("Engine and models installed; launch through the existing profile/queue flow")
    return {"machine": mid, "profile": profile, "release_id": release_id, "inventory": inventory,
            "starts_inference": False}
