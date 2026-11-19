"""LAN inventory, pairing and existing node provisioning jobs."""
from hydraweave import onboarding, provisioning
from hydraweave import bootstrap
import hashlib
import json
import time
from . import ops


def inventory(api, query):
    ops.strict(query, ())
    return api.document("discovery", **onboarding.of(api).inventory())


def adopt(api, arguments):
    ops.strict(arguments, ("id", "code"), ("id", "code"))
    result = onboarding.of(api).adopt(arguments["id"], arguments["code"])
    api._expires = 0
    cluster = api.cluster()
    if any(result["adopted"] in p.get("nodes", [p.get("rank0"), p.get("rank1")])
           for p in cluster.profiles.values()):
        try:
            result["provisioning"] = api.mutate("discovery/provision", {"machine": result["adopted"]})[1]
        except ValueError as exc:
            result["provisioning_error"] = str(exc)
    return result


def desired(api, query):
    if not query:
        return api.document("discovery-plan", plans=[], note="Choose a machine and its assigned profile")
    ops.strict(query, ("machine", "profile"), ("machine", "profile"))
    return api.document("discovery-plan", **provisioning.plan(api.cluster(), query["machine"], query["profile"]))


def _update_target(api, arguments):
    machine = api.cluster().machines[arguments["machine"]]
    saved = onboarding.credentials(api.config_path).get(machine.id)
    if machine.access != "agent" or not isinstance(saved, dict) or not saved:
        raise ValueError("Update node requires a paired node agent")
    # The resource id must keep naming the same machine and paired endpoint
    # between admission and execution. Only this digest enters jobs/audit.
    identity = hashlib.sha256(json.dumps({"machine": vars(machine), "pairing": saved},
                                        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return machine, saved, identity


def update_validate(api, arguments):
    ops.strict(arguments, ("machine", "origin"), ("machine", "origin"))
    bootstrap.script(arguments["origin"])
    _, _, identity = _update_target(api, arguments)
    return dict(arguments, target_identity=identity)


def update_node(api, arguments, ctx):
    machine, saved, identity = _update_target(api, arguments)
    if arguments.get("target_identity") != identity:
        raise ValueError("Node identity changed while the update was queued; review and try again")
    if ctx.cancelled():
        raise ValueError("Node update cancelled")
    ctx("Asking node agent to check GPU leases and re-pull the controller package")
    digest = hashlib.sha256(bootstrap.source_package()).hexdigest()
    # Re-adoption during a long update must not redirect later requests/polls
    # to another endpoint. The captured credential stays private to this job.
    client = onboarding.Client(machine, api.config_path, connection=saved)
    try:
        health = client.call("agent-health", {}, timeout=10)
    except PermissionError as error:
        if "unknown node verb" not in str(error):
            raise
        health = {}  # Pre-transaction agents report unknown verb through the existing error envelope.
    if health.get("update_protocol") != 1:
        raise ValueError("Node agent needs the transactional bootstrap migration; rerun bootstrap on the running node")
    previous = client.call("agent-update-status", {}, timeout=10)
    transaction = None
    try:
        result = client.call("agent-update", {"origin": arguments["origin"], "sha256": digest}, timeout=1900)
    except PermissionError:
        raise
    except (OSError, EOFError):
        # The independent worker may replace the listener before the final
        # response arrives. Recover only a new receipt for this exact package.
        pass
    else:
        if result.get("state") != "activating" or not result.get("transaction"):
            raise ValueError("Node agent did not return a supervised update transaction; readiness is unconfirmed")
        transaction = result["transaction"]
    ctx("Node package staged; waiting for the new authenticated agent or a verified rollback")
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        # Once scheduled, the independent node supervisor completes activation
        # or rollback even when the browser job is cancelled/disconnected.
        if ctx.cancelled():
            raise ValueError("Stopped waiting for node update; its activation/rollback supervisor is still running")
        try:
            state = client.call("agent-update-status", {}, timeout=5)
        except (OSError, ValueError):
            time.sleep(0.25)  # A bounded reconnection window while systemd replaces the listener.
            continue
        if transaction is None:
            if state.get("id") == previous.get("id") or state.get("sha256") != digest:
                time.sleep(0.25)
                continue
            transaction = state.get("id")
        if not transaction or state.get("id") != transaction or state.get("sha256") != digest:
            raise ValueError("Node update transaction identity changed")
        if state.get("state") == "ready":
            ctx("New node agent is authenticated and healthy")
            return state
        if state.get("state") in {"rolled_back", "failed", "rollback_failed"}:
            raise ValueError("Node update " + state["state"] + ": " + str(state.get("error", "health check failed")))
        time.sleep(0.25)
    raise ValueError("Node update health is still unconfirmed; inspect agent-update-status on the node")


def provision(api, arguments, ctx):
    ops.strict(arguments, ("machine", "profile"), ("machine",))
    cluster = api.cluster()
    mid = arguments["machine"]
    profiles = ([arguments["profile"]] if arguments.get("profile") else
                [name for name, row in cluster.profiles.items()
                 if mid in row.get("nodes", [row.get("rank0"), row.get("rank1")])])
    result = [provisioning.execute(cluster, api.config_path, mid, profile,
                                   report=ctx, cancelled=ctx.cancelled) for profile in profiles]
    api._expires = 0
    return result


def register(reg):
    reg.get("discovery", inventory)
    reg.get("discovery/plan", desired)
    reg.job("discovery/update", update_node, validate=update_validate, admission="bench",
            resources=lambda api, a: {a["machine"]}, deadline_s=2200)
    # Do not audit codes or credentials; the returned machine ID is safe to display.
    reg.post("discovery/adopt", adopt)
    reg.job("discovery/provision", provision, exclusive=True, deadline_s=86400)
    reg.capability("discovery", True, "LAN discovery, pairing and engine/model provisioning")
