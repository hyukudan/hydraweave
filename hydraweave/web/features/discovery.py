"""LAN inventory, pairing and existing node provisioning jobs."""
from hydraweave import onboarding, provisioning
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
    # Do not audit codes or credentials; the returned machine ID is safe to display.
    reg.post("discovery/adopt", adopt)
    reg.job("discovery/provision", provision, exclusive=True, deadline_s=86400)
    reg.capability("discovery", True, "LAN discovery, pairing and engine/model provisioning")
