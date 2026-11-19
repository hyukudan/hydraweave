"""Pinned model acquisition through reviewable plans and owned download jobs."""
from copy import deepcopy
import hashlib
import json
from pathlib import PurePosixPath
import re
import time

from hydraweave import assets as integrity, config, downloads, library_jobs, orchestrate, procs, telemetry
from hydraweave.web.api import JobFailed
from . import ops


def document(api, query):
    ops.strict(query, ())
    cluster = api.cluster()
    rows = []
    for profile in cluster.profiles:
        try:
            _, _, mids = orchestrate._profile(cluster, profile)
            sources = downloads.sources(cluster, profile)
        except (ValueError, KeyError) as exc:
            rows.append({"profile": profile, "error": str(exc)})
            continue
        for machine in mids:
            for name, source in sources.items():
                identity = source.get("merged") if source.get("merge") else source.get("files", [None])[0] if len(source.get("files", [])) == 1 else None
                why = source.get("why") if source.get("status") != "download" else \
                    "This source needs a derived artifact; download cannot declare it ready." if source.get("after") or not identity else None
                rows.append({"profile": profile, "machine": machine, "asset": name,
                             "source": source, "downloadable": why is None, "why": why,
                             "configured": cluster.assets.get(machine, {}).get(name)})
    return api.document("assets", assets=rows, confirmation="Review source, file paths, hashes and disk requirements before download.")


def _selection(api, arguments, confirm=False):
    return _select(api.cluster(), telemetry.existing_store(api), arguments, confirm)


def _select(cluster, store, arguments, confirm=False):
    """Validate and freeze only the inputs used by this asset acquisition."""
    keys = ("profile", "asset", "machine", "path", "confirm") if confirm else ("profile", "asset", "machine", "path")
    ops.strict(arguments, keys, keys)
    for key in ("profile", "asset", "machine"):
        config.identifier(arguments[key], key)
    _, _, mids = orchestrate._profile(cluster, arguments["profile"])
    if arguments["machine"] not in mids:
        raise ValueError("machine is not a member of this profile")
    machine = cluster.machines[arguments["machine"]]
    if machine.os != "linux":
        raise ValueError("node downloads require Linux")
    path = config.absolute(arguments["path"], "path", "linux")
    if PurePosixPath(path).name in (".", "..") or ".." in PurePosixPath(path).parts:
        raise ValueError("invalid asset path")
    directory = str(PurePosixPath(path).parent)
    roots = downloads.library_roots(cluster, store)[machine.id]
    authorized_roots = sorted({r["path"] for r in roots if downloads.inside_bases(machine, directory, [r["path"]])})
    if not authorized_roots:
        raise ValueError("choose a destination under a configured model root")
    source = downloads.sources(cluster, arguments["profile"]).get(arguments["asset"])
    if not source or source.get("status") != "download" or source.get("after"):
        raise ValueError((source or {}).get("why") or "this asset has no directly usable pinned download")
    pin = source.get("merged") if source.get("merge") else source["files"][0] if len(source["files"]) == 1 else None
    if not pin:
        raise ValueError("this asset has no pinned final artifact identity")
    if not source.get("merge") and PurePosixPath(path).name != PurePosixPath(pin["file"]).name:
        raise ValueError("destination filename must be " + PurePosixPath(pin["file"]).name)
    raw = deepcopy(cluster.raw)
    raw["assets"].setdefault(machine.id, {})[arguments["asset"]] = {"path": path, "bytes": pin["bytes"], "sha256": pin["sha256"]}
    proposed = config.parse(raw)
    # Transport and guard policy come entirely from this machine; the chosen
    # profile and catalog row determine membership and the immutable source.
    # Retain the previous asset record to avoid replacing an intervening edit,
    # but let other nodes/assets register independently in the shared config.
    material = {"profile": arguments["profile"], "settings": cluster.profiles[arguments["profile"]],
                "machine": orchestrate._machine_settings(cluster, machine.id),
                "asset": arguments["asset"], "existing": cluster.assets.get(machine.id, {}).get(arguments["asset"]),
                "source": source, "destination": path, "authorized_roots": authorized_roots}
    frozen = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return proposed, frozen


def prepare(api, arguments):
    cluster, _ = _selection(api, arguments)
    machine = cluster.machines[arguments["machine"]]
    directory = str(PurePosixPath(arguments["path"]).parent)
    roots = downloads.library_roots(api.cluster(), telemetry.existing_store(api))[machine.id]
    if not any(downloads.inside_bases(machine, directory, [r["path"]]) for r in roots):
        raise ValueError("choose a directory under a configured model root")
    return downloads._node(machine, "download-prepare", {**downloads._policy(cluster, machine), "directory": directory})


def plan(api, arguments):
    cluster, frozen = _selection(api, arguments)
    result = downloads.plan(cluster, arguments["profile"], arguments["asset"], arguments["machine"])
    return {**result, "path": arguments["path"], "configuration": frozen,
            "confirm": library_jobs.review_confirm(result["confirm"], frozen, 12)}


def validate(api, arguments):
    # Confirmation is rechecked by the node against the fresh immutable source
    # plan when it starts; callers cannot submit a custom URL or hash.
    cluster, frozen = _selection(api, arguments, confirm=True)
    if not isinstance(arguments["confirm"], str) or not re.fullmatch(r"[0-9a-f]{12}", arguments["confirm"]):
        raise ValueError("review the plan and send its confirmation code")
    return {**arguments, "configuration": frozen}


def resources(api, arguments):
    return {arguments["machine"]}


def run(api, arguments, ctx):
    original = {k: arguments[k] for k in ("profile", "asset", "machine", "path", "confirm")}
    cluster, frozen = _selection(api, original, confirm=True)
    if frozen != arguments["configuration"]:
        raise ValueError("configuration changed while the download was queued; review again")
    machine = cluster.machines[arguments["machine"]]
    planned = downloads.plan(cluster, arguments["profile"], arguments["asset"], machine.id)
    if library_jobs.review_confirm(planned["confirm"], frozen, 12) != arguments["confirm"]:
        raise ValueError("download plan or machine configuration changed; review and confirm again")
    started = downloads.start(cluster, arguments["profile"], arguments["asset"], machine.id, planned["confirm"], owned=True)
    state = None
    payload = {"root": machine.install_root, "job": started["job"]}
    poll = lambda renew: downloads._node(machine, "download-status", {**payload, "renew": renew}, timeout=10)
    cancel = lambda: downloads._node(machine, "download-cancel", payload, timeout=10)
    report = lambda row: ctx("Downloading " + arguments["asset"] + ": " + json.dumps(row.get("progress") or {}))
    try:
        state = library_jobs.watch_owned(ctx, report, poll, cancel)
        if ctx.cancelled():
            return None
        if state.get("state") != "succeeded":
            raise JobFailed(state.get("error") or "download did not succeed")
        pin = cluster.assets[machine.id][arguments["asset"]]
        entry = (state.get("verified") or {}).get(pin["path"])
        if (not isinstance(entry, dict) or entry.get("sha256") != pin["sha256"] or entry.get("bytes") != pin["bytes"]
                or any(type(entry.get(key)) is not int for key in integrity.FIELDS)):
            raise JobFailed("node did not return the verified final asset identity; configuration unchanged")
        fresh = downloads._node(machine, "stat-assets", {"paths": [pin["path"]]}, timeout=10).get("assets", {}).get(pin["path"], {})
        if ctx.cancelled():
            return None
        if any(fresh.get(key) != entry[key] for key in integrity.FIELDS):
            raise JobFailed("downloaded file changed before registration; configuration unchanged")
        entry = {key: entry[key] for key in (*integrity.FIELDS, "sha256")}
        # A short config transaction, while this job's admission hold still
        # excludes launches, preserves unrelated edits and refuses a stale plan.
        with ops._write_lock, ops.ConfigLock(api.config_path):
            raw, _ = ops._read_config(api)
            try:
                _, current = _select(config.parse(raw), telemetry.existing_store(api), original, confirm=True)
            except (ValueError, KeyError):
                raise JobFailed("download verified, but configuration changed; register the retained file after review") from None
            if current != frozen:
                raise JobFailed("download verified, but configuration changed; register the retained file after review")
            if ctx.cancelled():
                return None
            raw["assets"].setdefault(machine.id, {})[arguments["asset"]] = pin
            config.parse(raw)
            lock = procs.InstanceLock(integrity.manifest_path().with_suffix(".lock")).acquire()
            try:
                if ctx.cancelled():
                    return None
                manifest = integrity.read_manifest()
                manifest.setdefault(machine.id, {})[pin["path"]] = {**entry, "verified_at": time.time()}
                integrity.write_manifest(manifest)
            finally:
                lock.close()
            if ctx.cancelled():
                return None
            ops.write_config(api.config_path, raw)
        with api._poll_lock:
            api._expires = 0
        return {"machine": machine.id, "asset": arguments["asset"], "profile": arguments["profile"],
                "registered": True, "identity": pin, "job": started["job"],
                "bytes_transferred": state.get("bytes_transferred"), "bytes_reused": state.get("bytes_reused")}
    except (library_jobs.CleanupUnresolved, library_jobs.NodeJobFailed):
        raise
    except (Exception, KeyboardInterrupt) as exc:
        if state is None or state.get("state") in ("running", "queued"):
            ctx.cancel(str(exc) or "cancelled by operator")
            try:
                library_jobs.watch_owned(ctx, lambda row: None, poll, cancel)
            except library_jobs.CleanupUnresolved:
                raise
            except (OSError, ValueError, RuntimeError):
                pass
        raise


def register(reg):
    reg.get("assets", document)
    reg.post("assets/prepare", prepare, audited=True)
    reg.post("assets/plan", plan)
    reg.job("assets/download", run, validate=validate, admission="bench", resources=resources, deadline_s=86400)
    reg.capability("assets/download", True, "Pinned downloads with explicit plan confirmation and verified registration")
