"""Repository resources used by the editable control-machine installation."""

from pathlib import Path
import sys


_ROOT = Path(__file__).resolve().parent.parent


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
