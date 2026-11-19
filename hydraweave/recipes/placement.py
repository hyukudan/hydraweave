"""CPU placement orchestration over cached inputs. Tierstream is the only placement planner.

The diagnostic reader remains usable for editing recipes, but cannot place or solve. Missing/incompatible core
binaries, missing JSON output and PL_E_* refusals are terminal: there is no Python arithmetic or stub fallback.
This module neither probes hardware nor launches an engine.
"""
from copy import deepcopy

from . import calibration, commands, content_cache, core, session
from .reader import RecipeError


def _backend(cluster, backend):
    backend = backend if backend is not None else core.backend(cluster)
    if not isinstance(backend, core.CoreBackend):
        raise RecipeError("placement requires a compatible ts-recipe core (pc.recipe_core, HYDRAWEAVE_TS_RECIPE or PATH)")
    return backend


def place(path, *, route, probe, components, cluster=None, layers=None, backend=None, selected_content=None,
          cache=None, **kwargs):
    """Return the core's placement JSON and explanation from one invocation."""
    if content_cache.is_reference(components):
        components = (cache or commands.components_store()).load(components, selected=selected_content)
    return _backend(cluster, backend).place(path, layers or session.Layers(), route=route, probe=probe,
                                           components=components, **kwargs)


def solve(path, *, route, probe, inputs, cluster=None, layers=None, backend=None, selected_content=None,
          cache=None, measurement_keys=None, measurement_cache=None, performance_receipt=None, **kwargs):
    """Return the core's solver rows and table from one invocation."""
    calibration.performance(inputs)  # Validate the shape before resolving optional choices.
    if isinstance(inputs, dict):
        inputs = deepcopy(inputs)
        for choice in inputs.get("choices", []):
            if content_cache.is_reference(choice.get("components")):
                selected = choice.pop("selected_content", selected_content)
                choice["components"] = (cache or commands.components_store()).load(choice["components"], selected=selected)
    layers = layers or session.Layers()
    if performance_receipt is not None:
        import hashlib
        from pathlib import Path
        calibration.validate(performance_receipt, probe, inputs, route=route, workload=layers.workload,
                             context=kwargs.get("context") or {}, recipe_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                             settings=calibration.layer_settings(layers))
    else:
        inputs = calibration.without_performance(inputs)
    probe = cached_measurements(probe, measurement_keys or {}, measurement_cache)
    if isinstance(inputs, dict):
        for choice in inputs.get("choices", []):
            choice["probe_xfer"] = deepcopy(probe["probe_xfer"])
    return _backend(cluster, backend).solve(path, layers, route=route, probe=probe,
                                           inputs=inputs, **kwargs)


def cached_measurements(probe, keys, cache=None):
    """Replace measurement figures with exact current-key hits. Misses remain unpredicted in Tierstream.

    The caller supplies Tierstream's current xfer-v1/store identities; this path only reads local JSON.
    """
    from .measurements import Cache, bind_keys
    cache = cache or Cache(commands.runtime_dir())
    identities = bind_keys(probe, keys)
    report = deepcopy(probe)
    report["probe_xfer"] = {}
    for sid, identity in identities.items():
        row = cache.lookup(identity, "store" if sid == "store" else "xfer")
        if row["status"] == "measured":
            report["probe_xfer"][sid] = dict(row["values"], provenance=row["provenance"])
    return report
