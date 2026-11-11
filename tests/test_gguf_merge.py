"""Synthetic GGUFs only: no model downloads or accelerators."""
import hashlib
import io
import json
from pathlib import Path
import struct
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest

from hydraweave import cli, gguf_merge


def string(value):
    value = value.encode() if isinstance(value, str) else value
    return struct.pack("<Q", len(value)) + value


def split_part(path, index, tensors, *, count=2, total=3, alignment=32, include_alignment=True):
    fields = [("general.name", 8, string("synthetic")),
              ("general.alignment", 4, struct.pack("<I", alignment)),
              ("test.array", 9, struct.pack("<IQii", 5, 2, -1, 2)),
              ("split.no", 2, struct.pack("<H", index)),
              ("split.count", 2, struct.pack("<H", count)),
              ("split.tensors.count", 5, struct.pack("<i", total))]
    if not include_alignment:
        fields = [field for field in fields if field[0] != "general.alignment"]
    header = struct.pack("<4sIQQ", b"GGUF", 3, len(tensors), len(fields))
    for name, kind, value in fields:
        header += string(name) + struct.pack("<I", kind) + value
    payload = b""
    for name, body in tensors:
        payload += b"\0" * (-len(payload) % alignment)
        header += string(name) + struct.pack("<IQIQ", 1, len(body) // 4, 0, len(payload))
        payload += body
    path.write_bytes(header + b"\0" * (-len(header) % alignment) + payload)
    return path


def parts(tmp_path, alignment=32):
    return [split_part(tmp_path / "one.gguf", 0, [("a", b"A" * 12), ("b", b"B" * 20)], alignment=alignment),
            split_part(tmp_path / "two.gguf", 1, [("c", b"C" * 8)], alignment=alignment)]


@pytest.mark.parametrize("alignment", [32, 64, 256])
def test_round_trip_and_alignment(tmp_path, alignment):
    source = parts(tmp_path, alignment)
    destination = tmp_path / "merged.gguf"
    result = gguf_merge.merge(source, destination)
    body = destination.read_bytes()
    assert result == {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
    merged = gguf_merge._part(destination)
    assert merged["values"] == {"general.name": None, "general.alignment": alignment, "test.array": None}
    assert len(merged["tensors"]) == 3
    for tensor, expected in zip(merged["tensors"], [b"A" * 12, b"B" * 20, b"C" * 8]):
        assert tensor["offset"] % alignment == 0
        assert body[tensor["start"]:tensor["start"] + len(expected)] == expected
    assert not Path(str(destination) + ".partial").exists()
    assert all(path.exists() for path in source)


@pytest.mark.parametrize("bad", ["order", "missing", "total", "duplicate", "version", "truncated"])
def test_invalid_parts(tmp_path, bad):
    source = parts(tmp_path)
    if bad == "order":
        source.reverse()
    elif bad == "missing":
        source.pop()
    elif bad == "total":
        split_part(source[1], 1, [("c", b"C" * 8)], total=99)
    elif bad == "duplicate":
        split_part(source[1], 1, [("a", b"C" * 8)])
    elif bad == "version":
        source[1].write_bytes(b"GGUF" + struct.pack("<I", 2))
    else:
        source[1].write_bytes(source[1].read_bytes()[:40])
    with pytest.raises(ValueError, match="wrong part order or missing part|duplicate|GGUF|truncated"):
        gguf_merge.merge(source, tmp_path / "out.gguf")
    assert not (tmp_path / "out.gguf").exists()


def test_empty_metadata_part_and_source_alignment_change(tmp_path):
    source = [split_part(tmp_path / "one", 0, [], total=1, alignment=64),
              split_part(tmp_path / "two", 1, [("x", b"X" * 12)], total=1, alignment=32)]
    destination = tmp_path / "out"
    gguf_merge.merge(source, destination)
    parsed = gguf_merge._part(destination)
    tensor = parsed["tensors"][0]
    assert tensor["start"] % 64 == 0
    assert destination.read_bytes()[tensor["start"]:] == b"X" * 12


def test_default_alignment_and_cancelled_merge(tmp_path):
    source = [split_part(tmp_path / "one", 0, [("a", b"A" * 12)], total=2, include_alignment=False),
              split_part(tmp_path / "two", 1, [("b", b"B" * 12)], total=2, include_alignment=False)]
    destination = tmp_path / "out"
    gguf_merge.merge(source, destination)
    parsed = gguf_merge._part(destination)
    assert parsed["alignment"] == 32 and "general.alignment" not in parsed["values"]
    assert [tensor["offset"] for tensor in parsed["tensors"]] == [0, 32]
    previous = destination.read_bytes()

    def cancel():
        raise ValueError("cancelled")

    with pytest.raises(ValueError, match="cancelled"):
        gguf_merge.merge(source, destination, check=cancel)
    assert destination.read_bytes() == previous
    assert all(path.exists() for path in source)
    assert not Path(str(destination) + ".partial").exists()


def test_pin_failure_is_atomic_and_fsync_precedes_rename(tmp_path):
    source, destination = parts(tmp_path), tmp_path / "out"
    destination.write_bytes(b"original")
    with pytest.raises(ValueError, match="merged GGUF"):
        gguf_merge.merge(source, destination, expected={"bytes": 1, "sha256": "0" * 64})
    assert destination.read_bytes() == b"original"
    assert not Path(str(destination) + ".partial").exists()
    events = []
    replace = gguf_merge.os.replace
    with patch.object(gguf_merge.os, "fsync", side_effect=lambda fd: events.append("fsync")), \
            patch.object(gguf_merge.os, "replace", side_effect=lambda a, b: (events.append("rename"), replace(a, b))):
        pin = gguf_merge.merge(source, destination)
    assert events == ["fsync", "rename"]
    assert gguf_merge.merge(source, destination, expected=pin) == pin


def test_streamed_copy_and_stale_partial(tmp_path):
    source, destination = parts(tmp_path), tmp_path / "out"
    # A tiny copy chunk exercises multiple reads for these small tensors.
    with patch.object(gguf_merge, "CHUNK", 4):
        gguf_merge.merge(source, destination)
    partial = Path(str(destination) + ".partial")
    partial.write_bytes(b"other merge")
    with pytest.raises(FileExistsError):
        gguf_merge.merge(source, destination)
    assert partial.read_bytes() == b"other merge"


def test_cli_and_standalone_node_copy(tmp_path):
    source, destination = parts(tmp_path), tmp_path / "out"
    output = io.StringIO()
    with redirect_stdout(output):
        assert cli.main(["gguf-merge", *map(str, source), "-o", str(destination)]) == 0
    assert json.loads(output.getvalue())["sha256"] == hashlib.sha256(destination.read_bytes()).hexdigest()
    root = Path(__file__).resolve().parents[1]
    assert (root / "hydraweave/gguf_merge.py").read_bytes() == (root / "node/hydraweave_gguf_merge.py").read_bytes()
