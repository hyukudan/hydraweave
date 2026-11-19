"""Controller coordination for owned model verification and direct peer copies."""
import hashlib
import json
from pathlib import PurePosixPath
import time
import uuid

from . import downloads, orchestrate, remote


def selection(cluster, store, machine, model_root, name):
    node = downloads._machine(cluster, machine)
    downloads._root_of(cluster, node, model_root, store)
    downloads.models_module().unit_name(name)
    if node.os != "linux" and not downloads._is_local(node):
        raise ValueError("remote managed model jobs require a Linux node")
    return node


def _call(machine, verb, payload, timeout=45):
    return downloads._node(machine, verb, payload, timeout=timeout)


class CleanupUnresolved(RuntimeError):
    """The node's owner lease expired, but a final result was not observed."""


class NodeJobFailed(ValueError):
    """An observed terminal failed/cancelled node job."""


def identity(cluster, mids):
    """Freeze configured transport, roots and assets while a job is queued."""
    raw = cluster.raw
    machines = {m["id"]: m for m in raw["machines"]}
    material = {"machines": {m: machines[m] for m in sorted(mids)},
                "assets": {m: raw.get("assets", {}).get(m, {}) for m in sorted(mids)}}
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def review_confirm(native, configuration, length=16):
    """Bind the reviewed node plan to configured transport and resource identity."""
    return hashlib.sha256(("hydraweave-model-plan-v1:" + configuration + ":" + native).encode()).hexdigest()[:length]


def peer(cluster, source, destination, model_root):
    if source.id == destination.id:
        raise ValueError("select a different destination machine")
    if source.os != "linux" or source.access not in {"ssh", "agent"}:
        raise ValueError("direct copies require a managed Linux source node")
    target = source.peer_ssh.get(destination.id)
    if not target:
        raise ValueError(source.id + " has no configured peer_ssh route to " + destination.id)
    if destination.os != "linux" or destination.access != "ssh":
        raise ValueError("direct copies require a Linux SSH destination with a pinned host key")
    client = orchestrate._client(destination)
    if not isinstance(client, remote.Remote):
        raise ValueError("destination does not expose the pinned SSH transport")
    # options validates that BOTH alias/address are pinned. Only public host-key
    # material crosses to the source node, never controller credentials.
    client.known_hosts.options(client.alias, client.address, client.port)
    wanted = {remote._token(client.alias, client.port), remote._token(client.address, client.port)}
    keys = [parts[1] + " " + parts[2] for line in client.known_hosts._lines()
            if len(parts := line.split()) == 3 and wanted <= set(parts[0].split(","))]
    if len(set(keys)) != 1:
        raise ValueError("destination host key is ambiguous")
    tool = str(PurePosixPath(client._tool_path(15, None)).parent / "hydraweave_model_jobs.py")
    return {"target": target, "address": client.address, "port": client.port, "host_key": keys[0],
            "tool": tool, "policy": downloads._policy(cluster, destination), "model_root": model_root}


def copy_plan(cluster, store, arguments):
    source = selection(cluster, store, arguments["machine"], arguments["model_root"], arguments["name"])
    destination = selection(cluster, store, arguments["destination"], arguments["destination_root"], arguments["name"])
    payload = {**downloads._policy(cluster, source), "model_root": arguments["model_root"], "name": arguments["name"],
               "peer": peer(cluster, source, destination, arguments["destination_root"])}
    planned = _call(source, "copy-plan", payload)
    frozen = identity(cluster, {source.id, destination.id})
    return {"machine": source.id, "destination": destination.id, "name": arguments["name"],
            "files": [{k: r[k] for k in ("path", "bytes", "sha256")} for r in planned["files"]],
            "bytes": planned["bytes"], "confirm": review_confirm(planned["confirm"], frozen), "configuration": frozen,
            "transport": "direct SSH, source to destination", "controller_model_bytes": 0,
            "note": "Existing content and retained staging files are reused only after SHA-256 verification."}


def watch_owned(ctx, report, poll, cancel):
    """Observe termination, or retain ownership through the node's owner TTL."""
    cancelled = False
    cleanup_until = None
    last_error = None
    while True:
        if ctx.cancelled() and not cancelled:
            cancelled, cleanup_until = True, time.monotonic() + 155
        try:
            if cancelled:
                cancel()
            state = poll(not cancelled)
        except (OSError, ValueError, RuntimeError) as exc:
            last_error = str(exc)
            if not cancelled:
                # Stop renewing after any uncertain reply. Keep the admission
                # hold through the independent node owner TTL plus one bounded
                # request budget; the node's lifetime I/O lock remains decisive.
                cancelled, cleanup_until = True, time.monotonic() + 155
            if time.monotonic() >= cleanup_until:
                raise CleanupUnresolved("node cleanup unconfirmed after owner lease expiry; "
                                        "its model-I/O gate still protects resources: " + last_error) from exc
            time.sleep(.25)
            continue
        report(state)
        if state.get("state") not in ("queued", "running"):
            if state.get("state") != "succeeded":
                raise NodeJobFailed(state.get("error") or "model job " + str(state.get("state")))
            return state
        if cancelled and time.monotonic() >= cleanup_until:
            raise CleanupUnresolved("node has not confirmed cleanup after its owner lease expired; "
                                    "its model-I/O gate remains owned")
        if cancelled:
            time.sleep(0.2)
        elif ctx.wait(0.5):
            continue


def _wait(machine, policy, job, ctx, report):
    state = watch_owned(ctx, report,
                        lambda renew: _call(machine, "copy-job", {**policy, "job": job, "renew": renew}, timeout=10),
                        lambda: _call(machine, "copy-cancel", {**policy, "job": job}, timeout=10))
    return state["result"]


def execute(cluster, store, config_path, arguments, ctx, kind):
    machine = selection(cluster, store, arguments["machine"], arguments["model_root"], arguments["name"])
    if kind == "verify" and downloads._is_local(machine):
        return _verify_local(cluster, store, config_path, arguments, ctx)
    policy = downloads._policy(cluster, machine)
    payload = {**policy, "model_root": arguments["model_root"], "name": arguments["name"]}
    if kind == "copy":
        destination = selection(cluster, store, arguments["destination"], arguments["destination_root"], arguments["name"])
        payload.update(peer=peer(cluster, machine, destination, arguments["destination_root"]))
        planned = _call(machine, "copy-plan", payload)
        frozen = identity(cluster, {machine.id, destination.id})
        if review_confirm(planned["confirm"], frozen) != arguments["confirm"]:
            raise ValueError("copy plan or machine configuration changed; review and confirm again")
        payload["confirm"] = planned["confirm"]
    if downloads._is_local(machine):
        reason = downloads.ledger_guard(config_path, store).check()
        if reason:
            raise ValueError(reason)
    if ctx.cancelled():
        return None
    started = _call(machine, "copy" if kind == "copy" else "model-verify", payload)
    job = started["job"]
    row = {"id": job, "kind": kind, "machine": machine.id, "name": arguments["name"],
           "state": "running", "started": time.time(), "destination": arguments.get("destination")}

    def report(node):
        if downloads._is_local(machine):
            reason = downloads.ledger_guard(config_path, store).check()
            if reason:
                ctx.cancel(reason)
        row["node"] = node
        row["progress"] = json.dumps(node.get("progress") or {}, separators=(",", ":"))
        store.put("library/jobs", job, row)
        ctx(kind + " " + row["name"] + ": " + row["progress"])

    try:
        result = _wait(machine, policy, job, ctx, report)
        row.update(state="succeeded", result=result, at=time.time())
        return result
    except CleanupUnresolved as exc:
        row.update(state="unresolved", reason=str(exc), at=time.time())
        raise
    except NodeJobFailed as exc:
        row.update(state="cancelled" if ctx.cancelled() else "failed", reason=str(exc), at=time.time())
        raise
    except (Exception, KeyboardInterrupt) as exc:
        reason = str(exc) or "cancelled by operator"
        ctx.cancel(reason)
        try:
            _wait(machine, policy, job, ctx, lambda state: None)
        except CleanupUnresolved as cleanup:
            row.update(state="unresolved", reason=str(cleanup), at=time.time())
            raise cleanup from exc
        except (OSError, ValueError, RuntimeError):
            pass  # a failed/cancelled terminal node record is an observed stop
        row.update(state="cancelled" if ctx.cancelled() else "failed", reason=reason, at=time.time())
        raise
    finally:
        store.put("library/jobs", job, row)
        if kind == "verify":
            store.put("library/verify", machine.id + "|" + arguments["model_root"] + "|" + arguments["name"], row)


def _verify_local(cluster, store, config_path, arguments, ctx):
    """The controller's cache-free reader, with shared admission and ledger guard."""
    from . import paths, procs
    job = uuid.uuid4().hex
    row = {"id": job, "kind": "verify", "machine": arguments["machine"], "name": arguments["name"],
           "state": "running", "started": time.time()}
    guard = downloads.ledger_guard(config_path, store)
    next_check, next_progress = [0.0], [0.0]
    module = downloads.models_module()

    def check():
        if ctx.cancelled():
            raise module.Stopped(ctx.reason or "cancelled")
        if time.monotonic() >= next_check[0]:
            next_check[0] = time.monotonic() + .5
            reason = guard.check()
            if reason:
                raise module.Stopped(reason)

    def progress(count):
        if time.monotonic() < next_progress[0]:
            return
        next_progress[0] = time.monotonic() + .5
        row["progress"] = "hashed " + str(count) + " bytes"
        store.put("library/jobs", job, row)
        ctx(row["progress"])

    lock = procs.InstanceLock(paths.state_dir() / "library-model-io.lock")
    try:
        check()
        lock.acquire()
        result = module.verify_unit(arguments["model_root"], arguments["name"], check=check, progress=progress)
        check()
        row.update(state="succeeded", result=result)
        return result
    except Exception as exc:
        row.update(state="cancelled" if ctx.cancelled() else "failed", reason=str(exc))
        raise
    finally:
        lock.close()
        row["at"] = time.time()
        store.put("library/jobs", job, row)
        store.put("library/verify", arguments["machine"] + "|" + arguments["model_root"] + "|" + arguments["name"], row)
