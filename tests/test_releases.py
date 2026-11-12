"""Release transactions and update jobs without pip, systemd, GPUs or the network."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch
import urllib.error
import zipfile

import pytest

from hydraweave import cli, releases
from hydraweave.web.features import components


def wheel_at(directory, version="0.2.0", *, data="package"):
    directory.mkdir(parents=True, exist_ok=True)
    wheel = directory / f"hydraweave-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("hydraweave/__init__.py", data)
        archive.writestr("hydraweave/data/_build.json", json.dumps({"commit": "a" * 40}))
    (directory / "SHA256SUMS").write_text(hashlib.sha256(wheel.read_bytes()).hexdigest() + "  " + wheel.name + "\n")
    return wheel


def installed(manager, rid, created="2026-01-01"):
    record = manager.root / "releases" / rid
    record.mkdir(parents=True)
    python = manager.executable(rid, "python")
    python.parent.mkdir(parents=True)
    python.touch()
    (record / "release.json").write_text(json.dumps({"id": rid, "version": "0.1.0", "commit": "a" * 40,
                                                    "created": created, "wheel_sha256": "0" * 64}))
    return rid


def symlinks_or_skip(path):
    link = path / "symlink-probe"
    try:
        link.symlink_to("missing")
        link.unlink()
    except OSError:
        pytest.skip("symlink creation is unavailable (Windows requires developer mode or privileges)")


def test_stage_records_provenance_and_is_side_by_side(tmp_path):
    manager = releases.ReleaseManager(tmp_path / "prefix", runner=Mock(return_value=SimpleNamespace(returncode=0)))
    wheel = wheel_at(tmp_path / "wheels")
    rid = manager.stage(wheel, ["node"], offline=True)
    row, = manager.list()
    assert row["id"] == rid
    assert row["version"] == "0.2.0" and row["commit"] == "a" * 40
    assert row["wheel_sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert row["extras"] == ["node"] and not row["current"]
    argv = manager.runner.call_args_list[1].args[0]
    assert "--no-index" in argv and argv[-1].endswith("[node]")
    with pytest.raises(ValueError, match="already exists"):
        manager.stage(wheel)
    assert manager.environment(rid).is_dir()


def test_stage_failure_removes_only_directories_it_created(tmp_path):
    runner = Mock(side_effect=[SimpleNamespace(returncode=0), SimpleNamespace(returncode=1, stderr="pip failed")])
    manager = releases.ReleaseManager(tmp_path / "prefix", runner=runner)
    installed(manager, "existing")
    with pytest.raises(ValueError, match="pip failed"):
        manager.stage(wheel_at(tmp_path / "wheels"))
    assert [row["id"] for row in manager.list()] == ["existing"]
    assert [p.name for p in (manager.root / "venvs").iterdir()] == ["existing"]


def test_stage_rejects_unverified_wheel_and_dependencies_before_running(tmp_path):
    manager = releases.ReleaseManager(tmp_path / "prefix", runner=Mock())
    wheel = wheel_at(tmp_path / "wheels")
    (wheel.parent / "dependency-1-py3-none-any.whl").write_bytes(b"bad")
    with pytest.raises(ValueError, match="SHA256"):
        manager.stage(wheel, offline=True)
    manager.runner.assert_not_called()
    assert not manager.root.exists()
    wheel.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="SHA256"):
        manager.stage(wheel)


def test_activation_atomic_replace_and_shims_follow_current(tmp_path):
    symlinks_or_skip(tmp_path)
    manager = releases.ReleaseManager(tmp_path)
    installed(manager, "old")
    installed(manager, "new")
    manager.activate("old")
    manager.write_shims(True)
    shim = (tmp_path / "bin" / "hydraweave").read_bytes()
    observed = []
    original = os.replace
    def replace(source, destination):
        if Path(destination).name == "current":
            observed.append(manager.current)
            assert Path(source).is_symlink()
        return original(source, destination)
    with patch.object(releases.os, "replace", side_effect=replace):
        manager.activate("new")
    assert observed == ["old"] and manager.current == "new" and manager.previous == "old"
    assert (tmp_path / "bin" / "hydraweave").read_bytes() == shim
    assert "current" in shim.decode()
    assert not list(manager.root.glob(".current-*"))
    rows = {row["id"]: row for row in manager.list()}
    assert rows["new"]["current"] and rows["old"]["previous"]
    with pytest.raises(ValueError, match="current"):
        manager.remove("new")
    manager.remove("old")
    assert not manager.environment("old").exists() and manager.previous is None


def test_failed_atomic_replace_keeps_both_pointers(tmp_path):
    symlinks_or_skip(tmp_path)
    manager = releases.ReleaseManager(tmp_path)
    for rid in ("older", "old", "new"):
        installed(manager, rid)
    manager.activate("older")
    manager.activate("old")
    original = os.replace
    def replace(source, destination):
        if Path(destination).name == "current":
            raise OSError("replace failed")
        return original(source, destination)
    with patch.object(releases.os, "replace", side_effect=replace), pytest.raises(OSError):
        manager.activate("new")
    assert manager.current == "old" and manager.previous == "older"


def test_prune_keeps_current_previous_and_newest_to_count(tmp_path):
    symlinks_or_skip(tmp_path)
    manager = releases.ReleaseManager(tmp_path, keep=3)
    for number in range(5):
        installed(manager, f"r{number}", created=f"2026-01-0{number + 1}")
    manager.activate("r0")
    manager.activate("r1")
    manager.prune()
    assert {row["id"] for row in manager.list()} == {"r0", "r1", "r4"}
    assert not manager.environment("r2").exists()
    manager.keep = 2
    manager.prune()
    assert {row["id"] for row in manager.list()} == {"r0", "r1"}


def test_failed_health_rolls_back_and_reports_reason(tmp_path):
    symlinks_or_skip(tmp_path)
    manager = releases.ReleaseManager(tmp_path)
    for rid in ("older", "old", "new"):
        installed(manager, rid)
    manager.activate("older")
    manager.activate("old")
    calls, progress = [], []
    def health(rid):
        calls.append(rid)
        if rid == "new":
            raise ValueError("portal timed out")
    result = manager.apply_update(lambda: "new", health, report=progress.append)
    assert result["state"] == "rolled_back" and "portal timed out" in result["reason"]
    assert manager.current == "old" and manager.previous == "older"
    assert (manager.root / "current").resolve() == manager.environment("old")
    assert calls == ["new", "old"] and "rolled back" in progress[-1]


def test_initial_health_failure_leaves_no_active_pointer(tmp_path):
    symlinks_or_skip(tmp_path)
    manager = releases.ReleaseManager(tmp_path)
    installed(manager, "new")
    result = manager.apply_update(lambda: "new", lambda _: False, report=lambda _: None)
    assert result["state"] == "failed" and "no previous" in result["reason"]
    assert manager.current is None


@pytest.mark.parametrize("rid", ["../escape", "..", "/tmp/x", "x/y", "x\\y"])
def test_release_ids_cannot_escape_prefix(tmp_path, rid):
    manager = releases.ReleaseManager(tmp_path)
    for method in (manager.activate, manager.remove):
        with pytest.raises(ValueError, match="Invalid release id"):
            method(rid)


def test_cli_parsing_and_empty_release_check(capsys):
    parser = cli.build_parser()
    for operation in ("activate", "remove"):
        args = parser.parse_args(["releases", operation, "v1"])
        assert args.id == "v1" and args.operation == operation
    assert parser.parse_args(["releases", "list"]).operation == "list"
    args = parser.parse_args(["update", "--wheel", "release.whl", "--version", "0.2.0", "--check"])
    assert args.check and args.wheel == Path("release.whl") and args.version == "0.2.0"
    error = urllib.error.HTTPError("https://example.invalid", 404, "not found", {}, None)
    with patch.object(releases, "read_url", side_effect=error):
        assert cli.main(["update", "--check"]) == 0
    assert "no published release" in capsys.readouterr().out.lower()


def test_check_never_writes_or_fetches_wheel(tmp_path):
    manager = releases.ReleaseManager(tmp_path)
    with patch.object(releases, "release", return_value=("1.0", "wheel", {})), \
            patch.object(releases, "read_url") as fetch:
        assert releases.update(manager, check=True)["state"] == "available"
    fetch.assert_not_called()
    assert not manager.root.exists()


def test_health_checks_version_restarts_active_service_and_polls(tmp_path):
    manager = releases.ReleaseManager(tmp_path, runner=Mock(return_value=SimpleNamespace(returncode=0)))
    http, sleep = Mock(side_effect=[OSError("not ready"), False, True]), Mock()
    releases.health_check(manager, "new", service="hydraweave-portal.service", http=http, sleep=sleep)
    assert manager.runner.call_args_list[0].args[0][-1] == "--version"
    assert manager.runner.call_args_list[1].args[0] == ["systemctl", "--user", "restart", "hydraweave-portal.service"]
    assert http.call_count == 3 and sleep.call_count == 2
    with pytest.raises(ValueError, match="did not recover"):
        releases.health_check(manager, "new", service="portal", http=lambda _: False, sleep=sleep, attempts=2)


def test_update_runs_real_transaction_with_fake_runner_and_health(tmp_path):
    symlinks_or_skip(tmp_path)
    manager = releases.ReleaseManager(tmp_path / "prefix")
    installed(manager, "old")
    manager.activate("old")
    def run(argv, **kwargs):
        if "venv" in argv:
            python = Path(argv[-1]) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            python.parent.mkdir()
            python.touch()
        return SimpleNamespace(returncode=0)
    manager.runner = run
    with patch.object(releases, "portal_service", return_value=None):
        result = releases.update(manager, wheel=wheel_at(tmp_path / "wheels"), health_fn=lambda _: True)
    assert result["state"] == "updated" and manager.previous == "old"


def test_portal_update_job_returns_persisted_rollback(tmp_path):
    from hydraweave.web.api import API
    manager = releases.ReleaseManager(tmp_path / "prefix")
    api = API(config_path=tmp_path / "cluster.json", audit_path=tmp_path / "audit.jsonl")
    expected = {"state": "rolled_back", "current": "old", "reason": "health failed"}
    try:
        with patch.object(releases, "ReleaseManager", return_value=manager), \
                patch.object(releases, "portal_service", return_value=None), \
                patch.object(releases, "update", return_value=expected):
            job = api.mutate("components/update", {})
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                result = api.read("jobs/" + job["job"])
                if result["state"] != "running":
                    break
                time.sleep(0.01)
        assert result["result"]["state"] == "rolled_back"
        assert result["result"]["reason"] == "health failed"
        assert releases.update_status(manager)["state"] == "rolled_back"
    finally:
        api.close_features()


def test_portal_update_rejects_browser_paths_and_commands():
    for arguments in ({"wheel": "evil.whl"}, {"prefix": "/tmp"}, {"command": "x"}):
        with pytest.raises(ValueError):
            components.update_validate(None, arguments)


def test_portal_service_worker_survives_restart_and_persists_result(tmp_path):
    manager = releases.ReleaseManager(tmp_path)
    def runner(argv, **kwargs):
        assert argv[0] == "systemd-run" and "--collect" in argv
        assert argv[argv.index("--") + 1] == sys.executable
        job = argv[argv.index("--job") + 1]
        path = manager.root / "update-jobs" / (job + ".json")
        path.write_text(json.dumps({"state": "updated", "job": job}))
        return SimpleNamespace(returncode=0)
    manager.runner = runner
    with patch.object(releases, "portal_service", return_value="hydraweave-portal.service"):
        result = releases.portal_update(manager, sleep=Mock())
    assert result["state"] == "updated"


def test_os_lock_rejects_concurrent_release_operations_and_recovers(tmp_path):
    with releases.operation_lock(tmp_path):
        with pytest.raises(ValueError, match="Another release"):
            with releases.operation_lock(tmp_path):
                pytest.fail("concurrent lock acquired")
    with releases.operation_lock(tmp_path):
        pass


def test_health_endpoint_uses_no_gpu_or_cluster(tmp_path):
    from hydraweave.web.api import API
    from hydraweave.web.server import create_server
    from http.client import HTTPConnection
    import threading
    api = API(config_path=tmp_path / "missing.json", audit_path=tmp_path / "audit.jsonl")
    server = create_server(port=0, api=api)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        assert response.status == 200 and json.loads(response.read())["status"] == "ok"
        with patch.object(api, "read", return_value={"findings": []}) as read:
            connection.request("GET", "/hydraweave/api/health")
            response = connection.getresponse()
            assert response.status == 200 and json.loads(response.read()) == {"findings": []}
            read.assert_called_once_with("health", {})
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_health_rejects_a_portal_running_the_wrong_release(tmp_path):
    manager = releases.ReleaseManager(tmp_path, runner=Mock(return_value=SimpleNamespace(returncode=0)))
    installed(manager, "new")
    with pytest.raises(ValueError, match="did not recover"):
        releases.health_check(manager, "new", service="portal", attempts=1,
                              http=lambda _: {"status": "ok", "version": "0.0.0"})


def test_rollback_failure_is_reported_without_losing_original_reason(tmp_path):
    symlinks_or_skip(tmp_path)
    manager = releases.ReleaseManager(tmp_path)
    installed(manager, "old")
    installed(manager, "new")
    manager.activate("old")
    def unhealthy(rid):
        raise ValueError("broken " + rid)
    result = manager.apply_update(lambda: "new", unhealthy, report=lambda _: None)
    assert result["state"] == "rolled_back" and manager.current == "old"
    assert "broken new" in result["reason"] and "broken old" in result["reason"]


def test_update_current_wheel_is_a_verified_noop(tmp_path):
    symlinks_or_skip(tmp_path)
    wheel = wheel_at(tmp_path / "wheels")
    manager = releases.ReleaseManager(tmp_path / "prefix")
    rid = "v0.2.0-" + hashlib.sha256(wheel.read_bytes()).hexdigest()[:12]
    installed(manager, rid)
    manager.activate(rid)
    with patch.object(releases, "portal_service", return_value=None), patch.object(manager, "stage") as stage:
        result = releases.update(manager, wheel=wheel)
    assert result["state"] == "up_to_date"
    stage.assert_not_called()


def test_failed_update_still_prunes_older_unprotected_releases(tmp_path):
    symlinks_or_skip(tmp_path)
    manager = releases.ReleaseManager(tmp_path, keep=3)
    for index, rid in enumerate(("older", "old", "spare", "new")):
        installed(manager, rid, created=f"2026-01-0{index + 1}")
    manager.activate("older")
    manager.activate("old")
    result = manager.apply_update(lambda: "new", lambda rid: rid != "new", report=lambda _: None)
    assert result["state"] == "rolled_back"
    assert {row["id"] for row in manager.list()} == {"older", "old", "new"}


def test_prune_failure_does_not_misreport_a_healthy_activation(tmp_path):
    symlinks_or_skip(tmp_path)
    manager = releases.ReleaseManager(tmp_path)
    installed(manager, "new")
    with patch.object(manager, "prune", side_effect=OSError("directory busy")):
        result = manager.apply_update(lambda: "new", lambda _: True, report=lambda _: None)
    assert result["state"] == "updated" and "directory busy" in result["warning"]
