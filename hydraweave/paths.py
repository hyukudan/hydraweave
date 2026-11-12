"""Repository resources and user-scoped paths (no implicit migration)."""

import os
from pathlib import Path
import sys


_ROOT = Path(__file__).resolve().parent.parent


def user_dir(kind: str, *, home=None, environ=None, platform=None) -> Path:
    """HYDRAWEAVE_HOME contains config/state/data; existing node roots are independent."""
    home = Path.home() if home is None else Path(home)
    environ = os.environ if environ is None else environ
    platform = os.name if platform is None else platform
    if kind not in {"config", "state", "data"}:
        raise ValueError("unknown user directory kind")
    if environ.get("HYDRAWEAVE_HOME"):
        return Path(environ["HYDRAWEAVE_HOME"]).expanduser() / kind
    if platform == "nt":
        variable, fallback = (("APPDATA", "Roaming") if kind == "config" else ("LOCALAPPDATA", "Local"))
        base = Path(environ.get(variable) or home / "AppData" / fallback) / "hydraweave"
        return base / "state" if kind == "state" else base
    defaults = {"config": ".config", "state": ".local/state", "data": ".local/share"}
    value = environ.get("XDG_" + kind.upper() + "_HOME", "")
    # XDG ignores relative environment values.
    base = Path(value) if value and Path(value).is_absolute() else home / defaults[kind]
    return base / "hydraweave"


def config_dir(**kwargs) -> Path:
    return user_dir("config", **kwargs)


def state_dir(**kwargs) -> Path:
    return user_dir("state", **kwargs)


def data_dir(**kwargs) -> Path:
    return user_dir("data", **kwargs)


def legacy_config_dir(*, home=None, environ=None, platform=None) -> Path:
    home = Path.home() if home is None else Path(home)
    environ = os.environ if environ is None else environ
    platform = os.name if platform is None else platform
    if platform == "nt":
        return Path(environ.get("APPDATA") or home / "AppData/Roaming") / "hydraweave"
    return home / ".config/hydraweave"


def read_user_path(path: Path, *fallbacks: Path) -> Path:
    from .compat import read_path
    for candidate in (path, *fallbacks):
        found = read_path(candidate)
        if found.exists() or found.is_symlink():
            return found
    return path


def runtime_dir(*, config=None) -> Path:
    """New runtime state uses state/run; reuse an existing workspace's run directory."""
    if config is not None:
        directory = Path(config).parent
        if directory not in {config_dir(), legacy_config_dir()}:
            return directory / "run"
    old = (Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "hydraweave/run"
           if os.name == "nt" else legacy_config_dir() / "run")
    fallbacks = [old]
    if config is not None:
        fallbacks.insert(0, Path(config).parent / "run")
    return read_user_path(state_dir() / "run", *fallbacks)


def ensure_owned(path: Path) -> None:
    """Mark only a newly created product directory, never claim an existing directory."""
    path = Path(path)
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        (path / ".hydraweave-root").touch(exist_ok=False)


def prepare_write(path: Path) -> None:
    """Give newly created user roots an uninstall marker before writing below them."""
    path = Path(path)
    for root in (config_dir(), state_dir(), data_dir()):
        if path.is_relative_to(root):
            ensure_owned(root)


def repo_root():
    installed = Path(sys.prefix) / "share" / "hydraweave"
    if not (_ROOT / "node").is_dir() and (installed / "node").is_dir():
        return installed
    missing = [name for name in ("node", "versions.lock")
               if not ((_ROOT / name).is_dir() if name == "node" else (_ROOT / name).is_file())]
    if missing:
        raise RuntimeError("Hydraweave repository resources missing: " + ", ".join(missing)
                           + "; install hydraweave from its checkout with pip install -e .")
    return _ROOT
