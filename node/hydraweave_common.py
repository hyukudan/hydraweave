"""Shared, dependency-free helpers for the node tools."""
import json
import os
from pathlib import Path
import tempfile
import time

# Helpers only: listed in hydraweave-node EXTENSIONS so bootstrap ships it, but it serves no verbs.
VERBS = frozenset()


def atomic_bytes(path, data, *, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            if mode is not None and os.name != "nt":
                os.chmod(temporary, mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_json(path, value, *, mode=0o600, canonical=None):
    data = canonical(value) if canonical else json.dumps(value, allow_nan=False).encode("utf-8")
    atomic_bytes(path, data + b"\n", mode=mode)
