"""Public-only starter provisioning with fake node jobs."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from hydraweave import config, downloads, provisioning, render
from tests.test_gguf_merge import parts
from tests.test_downloads import node


def cluster():
    return config.parse({"schema": "hydraweave/cluster-v1", "machines": [
        {"id": "node1", "os": "linux", "access": "agent", "http_address": "192.0.2.1",
         "install_root": "/opt/hydraweave", "lab_mode": False, "devices": [{"id": "gpu", "type": "amd"}]}],
        "assets": {}, "profiles": {"starter": {"preset": "qwen-starter", "nodes": ["node1"]}}})


def test_public_assets_and_zero_blockers():
    source = downloads.sources(cluster(), "starter")
    assert set(source) == {"model"}
    for row in source.values():
        assert row["status"] == "download" and len(row["revision"]) == 40
        assert all(len(item["sha256"]) == 64 and item["bytes"] > 0 for item in row["files"])
    desired = provisioning.plan(cluster(), "node1", "starter")
    assert desired["blockers"] == []
    assert "first-time pin" in desired["models"][0]["pin"]
    starter = config.preset("qwen-starter")
    assert "starter: correct but not the tuned speed" in starter["receipt_note"]
    assert "{tokens}" not in starter["argv"] and "--mtp" not in starter["argv"]
    assert starter["argv"][:3] == ["{binary}", "{model}", "0"]
    assert "--mtp" in config.preset("qwen-replicas")["argv"]


def test_starter_renders_without_calibration_or_tuned_curves():
    parsed = cluster()
    parsed.profiles["starter"].update(release_id="a" * 12, api_key_files={"node1": "/opt/keys/serve"})
    parsed.assets["node1"] = {"model": {"path": "/opt/models/model.gguf", "bytes": 1, "sha256": "b" * 64}}
    process = render.render_processes(parsed, "starter")[0]
    assert process.argv[1:3] == ["/opt/models/model.gguf", "0"]
    assert "--http-serve" in process.argv and "--mtp" not in process.argv
    assert process.env == {} and "router" not in config.preset("qwen-starter")


def test_pinned_merge_plan_and_manual_after():
    source = downloads.sources(cluster(), "starter")
    source["model"]["merged"] = {"bytes": 1234, "sha256": "a" * 64}
    with patch.object(downloads, "sources", return_value=source):
        desired = provisioning.plan(cluster(), "node1", "starter")
        assert desired["manifests"]["model"]["sha256"] == "a" * 64
        source["model"]["after"] = "manual conversion"
        assert provisioning.plan(cluster(), "node1", "starter")["blockers"]


@pytest.mark.parametrize("change", ["merge", "merged", "digest", "size"])
def test_source_schema_rejects_invalid_merge_pins(change):
    catalog = deepcopy(downloads.catalog())
    row = catalog["profiles"]["qwen-starter"]["model"]
    if change == "merge":
        row["merge"] = "shell-command"
    elif change == "merged":
        row["merged"] = {"bytes": 1}
    elif change == "digest":
        row["merged"] = {"bytes": 1, "sha256": "not-a-digest"}
    else:
        row["files"][0]["bytes"] = -1
    read = downloads.read_json
    with patch.object(downloads, "read_json", side_effect=lambda path: catalog if path.name == "sources.json" else read(path)):
        with pytest.raises(config.ConfigError):
            downloads.catalog()


@pytest.mark.parametrize("bad_pin", [False, True])
def test_node_download_merge_and_part_retention(tmp_path, bad_pin):
    root = tmp_path / "install"
    node.install.checked_root({"root": str(root)}, create=True)
    directory = tmp_path / "assets"
    directory.mkdir()
    (directory / node.ASSET_MARKER).touch()
    inputs = parts(tmp_path)
    files = [{"destination": str(directory / path.name), "bytes": path.stat().st_size,
              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in inputs]
    spec = root / "run" / "job-test.spec.json"
    destination = directory / "out.gguf"
    node.install.atomic_json(spec, {"root": str(root), "job": "test", "files": files,
        "merge": {"kind": "gguf-split", "destination": str(destination),
                  "expected": {"bytes": 1, "sha256": "0" * 64} if bad_pin else None}})

    def fetch(row, *args):
        Path(row["destination"]).write_bytes(inputs[files.index(row)].read_bytes())

    with patch.object(node, "fetch", side_effect=fetch), patch.object(node, "lab_quiet"), patch.object(node, "no_lease"):
        assert node.job_main(str(spec)) == int(bad_pin)
    state = node.install.read_json(root / "run" / "job-test.json")
    if bad_pin:
        assert all(Path(row["destination"]).exists() for row in files)
        assert not destination.exists()
        assert "mismatch" in state["error"]
        # Retry uses retained, reverified parts without downloading them again.
        spec_data = {"root": str(root), "job": "retry", "files": files,
                     "merge": {"kind": "gguf-split", "destination": str(destination), "expected": None}}
        node.install.atomic_json(spec, spec_data)
        with patch.object(node, "fetch", side_effect=AssertionError("retained part redownloaded")), \
                patch.object(node, "lab_quiet"), patch.object(node, "no_lease"):
            assert node.job_main(str(spec)) == 0
    else:
        assert state["merged"]["sha256"] == hashlib.sha256(destination.read_bytes()).hexdigest()
        assert not any(Path(row["destination"]).exists() for row in files)


def test_execute_records_first_time_pin(tmp_path):
    parsed = cluster()
    raw = deepcopy(parsed.raw)
    path = tmp_path / "cluster.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    desired = provisioning.plan(parsed, "node1", "starter")
    expected = {"bytes": 1234, "sha256": "c" * 64}
    calls, reports = [], []

    class Client:
        def __init__(self, *args):
            pass

        def call(self, verb, payload):
            calls.append((verb, deepcopy(payload)))
            if verb == "download-plan":
                assert payload["merge"]["expected"] is None
                assert len(payload["files"]) == 4
                return {"disk": [{"ok": True}], "confirm": "a" * 12}
            if verb == "download":
                return {"job": "download"}
            if verb == "install":
                return {"job": "install"}
            if verb == "job":
                return {"state": "succeeded", "merged": expected, "release_id": "a" * 12}
            return {}

    with patch.object(provisioning, "Client", Client), \
            patch.object(provisioning, "plan", return_value=desired), \
            patch.object(provisioning.orchestrate, "_stage_bundles", return_value={}), \
            patch.object(provisioning.orchestrate, "_components", return_value={}):
        provisioning.execute(parsed, path, "node1", "starter", report=reports.append)
    manifest = config.read_json(path)["assets"]["node1"]["model"]
    assert {key: manifest[key] for key in expected} == expected
    assert any("first-time pin sha256=" + expected["sha256"] in line for line in reports)
    assert not {"lease", "peer-run", "service-start"} & {verb for verb, _ in calls}
