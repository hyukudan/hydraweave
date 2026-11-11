"""Native GGUF v3 split merge; tensor payloads never enter memory together.

The node ships an identical standalone copy, checked by the unit tests.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import struct
from typing import BinaryIO, Callable, Sequence

VERBS: frozenset[str] = frozenset()  # Also shipped as a node helper module.
CHUNK = 1024 * 1024
_SCALARS = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i",
            6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}


def _read(stream: BinaryIO, size: int) -> bytes:
    if size < 0 or size > os.fstat(stream.fileno()).st_size - stream.tell():
        raise ValueError("truncated GGUF metadata or tensor data")
    data = stream.read(size)
    if len(data) != size:
        raise ValueError("truncated GGUF")
    return data


def _number(stream: BinaryIO, fmt: str) -> int | float:
    return struct.unpack("<" + fmt, _read(stream, struct.calcsize("<" + fmt)))[0]


def _string(stream: BinaryIO) -> bytes:
    return _read(stream, _number(stream, "Q"))


def _value(stream: BinaryIO, kind: int, depth: int = 0):
    if depth > 16:
        raise ValueError("GGUF metadata array nesting too deep")
    if kind in _SCALARS:
        return _number(stream, _SCALARS[kind])
    if kind == 8:
        _string(stream)
        return None
    if kind == 9:
        subtype, count = _number(stream, "I"), _number(stream, "Q")
        if subtype in _SCALARS:
            size = count * struct.calcsize("<" + _SCALARS[subtype])
            if size > os.fstat(stream.fileno()).st_size - stream.tell():
                raise ValueError("truncated GGUF array")
            stream.seek(size, os.SEEK_CUR)
            return None
        if count > os.fstat(stream.fileno()).st_size - stream.tell():
            raise ValueError("truncated GGUF array")
        for _ in range(count):
            _value(stream, subtype, depth + 1)
        return None
    raise ValueError(f"unknown GGUF metadata type: {kind}")


def _align(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


def _part(path: Path) -> dict:
    with path.open("rb") as stream:
        if _read(stream, 4) != b"GGUF" or _number(stream, "I") != 3:
            raise ValueError(f"{path.name}: expected little-endian GGUF v3")
        count, kv_count = _number(stream, "Q"), _number(stream, "Q")
        values, kv = {}, []
        for _ in range(kv_count):
            start = stream.tell()
            key = _string(stream).decode("utf-8")
            if key in values:
                raise ValueError(f"duplicate GGUF metadata key: {key}")
            kind = _number(stream, "I")
            values[key] = _value(stream, kind)
            end = stream.tell()
            stream.seek(start)
            raw = _read(stream, end - start)
            if not key.startswith("split."):
                kv.append(raw)
        alignment = values.get("general.alignment", 32)
        if type(alignment) is not int or not 1 <= alignment <= 1024 * 1024 or alignment & (alignment - 1):
            raise ValueError("general.alignment must be a positive power of two <= 1048576")
        tensors = []
        for _ in range(count):
            start = stream.tell()
            name = _string(stream)
            dimensions = _number(stream, "I")
            if not 1 <= dimensions <= 4:
                raise ValueError("GGUF tensor must have 1..4 dimensions")
            for _ in range(dimensions):
                if not _number(stream, "Q"):
                    raise ValueError("GGUF tensor dimensions must be positive")
            _number(stream, "I")  # ggml type, preserved verbatim
            end = stream.tell()
            offset = _number(stream, "Q")
            stream.seek(start)
            info = _read(stream, end - start)
            stream.seek(end + 8)
            tensors.append({"name": name, "info": info, "offset": offset})
        data_start = _align(stream.tell(), alignment)
        size = os.fstat(stream.fileno()).st_size
        ordered = sorted(tensors, key=lambda tensor: tensor["offset"])
        for index, tensor in enumerate(ordered):
            offset = tensor["offset"]
            limit = ordered[index + 1]["offset"] if index + 1 < len(ordered) else size - data_start
            if offset % alignment or offset < 0 or limit <= offset or data_start + limit > size:
                raise ValueError("invalid GGUF tensor offset or truncated tensor extent")
            # Preserve each payload extent, including any source padding. No ggml type table is needed;
            # offsets in the new file identify the tensors independently of the preserved padding.
            tensor.update(start=data_start + offset, size=limit - offset, path=path)
        return {"values": values, "kv": kv, "tensors": tensors, "alignment": alignment}


def merge(parts: Sequence[str | Path], destination: str | Path, *, expected: dict | None = None,
          check: Callable[[], None] = lambda: None) -> dict:
    """Merge ordered parts atomically, returning ``bytes`` and ``sha256``.

    An optional expected pin is checked before promotion. Inputs are never deleted here.
    """
    paths, dest = [Path(path) for path in parts], Path(destination)
    partial = Path(str(dest) + ".partial")
    if not paths or any(path.resolve() in (dest.resolve(), partial.resolve()) for path in paths):
        raise ValueError("GGUF merge needs parts and a separate output path")
    parsed = [_part(path) for path in paths]
    total = sum(len(part["tensors"]) for part in parsed)
    names = set()
    for index, part in enumerate(parsed):
        values = part["values"]
        required = {"split.count": len(paths), "split.no": index, "split.tensors.count": total}
        for key, value in required.items():
            if type(values.get(key)) is not int or values[key] != value:
                raise ValueError(f"{paths[index].name}: {key} mismatch; wrong part order or missing part")
        for tensor in part["tensors"]:
            if tensor["name"] in names:
                raise ValueError("duplicate GGUF tensor name across split parts")
            names.add(tensor["name"])
    tensors = [tensor for part in parsed for tensor in part["tensors"]]
    alignment, kv = parsed[0]["alignment"], parsed[0]["kv"]
    offset = 0
    for tensor in tensors:
        tensor["new_offset"] = _align(offset, alignment)
        offset = tensor["new_offset"] + tensor["size"]
    digest, written = hashlib.sha256(), 0

    def write(output: BinaryIO, data: bytes) -> None:
        nonlocal written
        output.write(data)
        digest.update(data)
        written += len(data)

    def pad(output: BinaryIO, end: int) -> None:
        while written < end:
            write(output, b"\0" * min(CHUNK, end - written))

    created = False
    try:
        # Exclusive creation avoids following stale partial symlinks or clobbering another merge.
        with partial.open("xb") as output:
            created = True
            write(output, struct.pack("<4sIQQ", b"GGUF", 3, total, len(kv)))
            for raw in kv:
                write(output, raw)
            for tensor in tensors:
                write(output, tensor["info"] + struct.pack("<Q", tensor["new_offset"]))
            start = _align(written, alignment)
            pad(output, start)
            for tensor in tensors:
                pad(output, start + tensor["new_offset"])
                with tensor["path"].open("rb") as source:
                    source.seek(tensor["start"])
                    remaining = tensor["size"]
                    while remaining:
                        check()
                        data = _read(source, min(CHUNK, remaining))
                        write(output, data)
                        remaining -= len(data)
            result = {"bytes": written, "sha256": digest.hexdigest()}
            if expected is not None and any(result[key] != expected[key] for key in ("bytes", "sha256")):
                raise ValueError("merged GGUF bytes or sha256 mismatch")
            check()
            output.flush()
            os.fsync(output.fileno())
        os.replace(partial, dest)
        return result
    except BaseException:
        if created:
            partial.unlink(missing_ok=True)
        raise
