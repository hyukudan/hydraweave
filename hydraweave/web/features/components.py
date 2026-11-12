"""Component inventory, explicit upstream checks and cancellable source staging.

GET components and components/plan never call a node or the network. Background
jobs only touch the controller's marked component cache. Node builds/activation
continue through the existing install job and its admission checks.
"""
from hydraweave import components as core
from hydraweave import releases
from hydraweave.web.features import machines


def _arguments(value, allowed=(), required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ValueError("invalid component arguments")
    return dict(value)


def _cluster(api):
    try:
        return api.cluster(), None
    except (OSError, ValueError, RuntimeError) as exc:
        return None, str(exc)


def manager(api):
    return core.Manager(api.config_path, clock=api.clock)


def overview(api, query):
    _arguments(query)
    cluster, config_error = _cluster(api)
    try:
        launches = machines.launch_records(api) if cluster is not None else {}
    except (OSError, ValueError, RuntimeError):
        launches = {}
    try:
        document = manager(api).state(cluster, config_error=config_error, launches=launches)
    except (OSError, ValueError, RuntimeError) as exc:
        rows, _ = core.catalog()
        document = {"components": [dict(row, upstream=None, downloads=[], active=[], update_available=None,
                                        download={"enabled": False, "why": str(exc)}) for row in rows],
                    "profiles": [], "storage": {"scope": "controller", "path": None, "error": str(exc)},
                    "config_error": config_error, "checks_are_read_only": True, "activation_is_automatic": False}
    document["release_update"] = releases.update_status(releases.ReleaseManager())
    return api.document("components", **document)


def update_validate(api, arguments):
    return _arguments(arguments)


def update(api, arguments, ctx):
    _arguments(arguments)
    return releases.portal_update(releases.ReleaseManager(), report=ctx, cancelled=ctx.cancelled,
                                  url=getattr(api, "portal_health_url", "http://127.0.0.1:18700/api/health"))


def plan(api, query):
    query = _arguments(query, ("profile",))
    cluster, error = _cluster(api)
    if cluster is None:
        return api.document("components-plan", plans=[], config_error=error,
                            why={"plans": "Add and configure your nodes before installing a profile."})
    if "profile" in query:
        return api.document("components-plan", **core.install_plan(cluster, query["profile"]), config_error=None)
    return api.document("components-plan", plans=[core.install_plan(cluster, name) for name in cluster.profiles],
                        config_error=None)


def check_validate(api, arguments):
    arguments = _arguments(arguments, ("component",))
    if "component" in arguments:
        core.component(arguments["component"])
    manager(api)  # reject an invalid operator-supplied cache path before accepting a job
    return arguments


def check(api, arguments, ctx):
    result = manager(api).check(arguments.get("component"), report=ctx, cancelled=ctx.cancelled)
    if result["errors"]:
        from hydraweave.web.api import JobFailed
        raise JobFailed(f"{result['errors']} component update check(s) failed; see the component cards for details")
    return result


def download_validate(api, arguments):
    arguments = _arguments(arguments, ("component", "commit"), ("component", "commit"))
    core.component(arguments["component"])
    core.revision(arguments["commit"])
    cluster, _ = _cluster(api)
    manager(api).allowed(arguments["component"], arguments["commit"], cluster)
    return arguments


def download(api, arguments, ctx):
    cluster, _ = _cluster(api)
    return manager(api).download(arguments["component"], arguments["commit"], cluster,
                                 report=ctx, cancelled=ctx.cancelled)


def register(reg):
    reg.get("components", overview)
    reg.get("components/plan", plan)
    reg.job("components/update", update, validate=update_validate, exclusive=True, deadline_s=3600)
    reg.job("components/check", check, validate=check_validate, deadline_s=240)
    reg.job("components/download", download, validate=download_validate, deadline_s=1900)
    reg.capability("components", True, "Explicit upstream checks and immutable source downloads; profile installs stay pinned")
