"""Read compatibility for installations made before the project rename."""
import os
from pathlib import Path
import sys

LEGACY_NAME = "hyperloom"
_NOTICES = set()


def notice(feature):
    """Emit one line per deprecated feature, without values or user paths."""
    if feature not in _NOTICES:
        _NOTICES.add(feature)
        print(f"{LEGACY_NAME} {feature} is deprecated; use Hydraweave names and paths.", file=sys.stderr)


def getenv(name, default=None, *, environ=None):
    environ = os.environ if environ is None else environ
    if name in environ:
        return environ[name]
    old = name.replace("HYDRAWEAVE_", LEGACY_NAME.upper() + "_", 1)
    if old != name and old in environ:
        notice("environment variable " + old)
        return environ[old]
    return default


def legacy_path(path):
    path = Path(path)
    parts = list(path.parts)
    # Change the nearest product directory, preserving ancestors and filenames.
    for index in range(len(parts) - 2, -1, -1):
        if parts[index] in ("hydraweave", LEGACY_NAME):
            parts[index] = LEGACY_NAME
            break
    return Path(*parts)


def read_path(path):
    """Prefer the current path; fall back without moving or deleting data."""
    path = Path(path)
    old = legacy_path(path)
    if not (path.exists() or path.is_symlink()) and old != path and (old.exists() or old.is_symlink()):
        notice("config/state path")
        return old
    return path


def glob_paths(directory, pattern):
    """Merge state listings, preferring the current name for duplicate records."""
    directory = Path(directory)
    paths = {p.name: p for p in legacy_path(directory).glob(pattern)}
    paths.update({p.name: p for p in directory.glob(pattern)})
    return sorted(paths.values())


def schema(value):
    if isinstance(value, str) and value.startswith(LEGACY_NAME + "/"):
        notice("schema")
        return "hydraweave/" + value.split("/", 1)[1]
    return value


def api_path(path):
    prefix = "/" + LEGACY_NAME + "/"
    if path.startswith(prefix):
        notice("API path")
        return "/hydraweave/" + path[len(prefix):]
    return path


def legacy_document(value):
    if isinstance(value, dict):
        return {k: (v.replace("hydraweave/", LEGACY_NAME + "/", 1)
                    if k == "schema" and isinstance(v, str) and v.startswith("hydraweave/")
                    else legacy_document(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [legacy_document(v) for v in value]
    return value
