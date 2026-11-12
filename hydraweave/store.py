"""Small shared persistence helpers for controller state and configuration."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import threading
import time
import uuid


_write_lock = threading.Lock()


def retry_sharing(operation, attempts: int = 5):
    for index in range(attempts):
        try:
            return operation()
        except PermissionError:
            if index + 1 == attempts:
                raise
            time.sleep(0.02 * (index + 1))


def read_json(path: Path):
    return retry_sharing(lambda: json.loads(Path(path).read_text(encoding="utf-8")))


def atomic_write(path: Path, data: bytes, mode: int | None = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    file_mode = mode if mode is not None else (stat.S_IMODE(path.stat().st_mode) if path.exists() else None)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(temporary, flags, file_mode if file_mode is not None else 0o666)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        retry_sharing(lambda: os.replace(temporary, path))
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, value, *, mode: int | None = None) -> None:
    atomic_write(Path(path), (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8"), mode)


def write_config(path, raw):
    from .config import MAX_CONFIG_BYTES
    path = Path(path)
    data = (json.dumps(raw, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if len(data) > MAX_CONFIG_BYTES:
        raise ValueError("configuration would exceed 256 KiB")
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    if path.exists():
        previous = path.with_name(path.name + ".prev")
        retry_sharing(lambda: previous.write_bytes(path.read_bytes()))
    atomic_write(path, data, mode)
    return hashlib.sha256(data).hexdigest()


class ConfigLock:
    """Cross-process lock around a configuration edit."""

    def __init__(self, config_path, wait_s=5.0):
        from . import procs
        self.lock = procs.InstanceLock(Path(config_path).with_name(Path(config_path).name + ".lock"))
        self.wait_s = wait_s

    def __enter__(self):
        from . import procs
        end = time.monotonic() + self.wait_s
        while True:
            try:
                self.lock.acquire()
                return self
            except procs.ProcessError:
                if time.monotonic() >= end:
                    raise PermissionError("another process is editing the configuration; retry") from None
                time.sleep(0.1)

    def __exit__(self, *exc):
        self.lock.close()
