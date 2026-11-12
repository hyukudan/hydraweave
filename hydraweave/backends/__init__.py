"""Backend registry. Modules are imported lazily by name, like portal features."""
import importlib

MODULES = ("qwen", "ds41", "minimax", "llamacpp")
_registry = None

def registry():
    global _registry
    if _registry is None:
        _registry = {module: importlib.import_module(f"{__name__}.{module}").BACKEND for module in MODULES}
    return dict(_registry)

def get(name):
    rows = registry()
    if name in rows:
        return rows[name]
    for backend in rows.values():
        if name in backend.families:
            return backend
    raise KeyError(name)

def for_family(family):
    return get(family)

def model_registry(repo_dir=None, user_dir=None):
    from pathlib import Path
    from .base import load_json_records
    rows = {}
    for directory in (repo_dir or Path(__file__).resolve().parent.parent / "models", user_dir):
        if directory:
            rows.update(load_json_records(directory))
    return rows
