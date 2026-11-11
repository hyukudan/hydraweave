from unittest.mock import patch

from hydraweave import discovery
from hydraweave.version import version_status


def test_version_comparison_ignores_local_and_git_describe_suffixes():
    assert version_status("1.2.3+local", "1.2.3") == "ok"
    assert version_status("v1.2.3-4-gabcdef-dirty", "v1.2.3") == "ok"
    assert version_status("1.2.4", "1.2.3") == "outdated"
    assert version_status("", "1.2.3") == "unknown"


def test_discovery_rows_include_version_status():
    registry = discovery.Registry()
    registry.update({"id": "node1", "agent_version": "0.1.0+dev"})
    assert registry.rows()[0]["version_status"] in {"ok", "outdated"}


def test_version_comes_from_pyproject_not_a_bare_git_hash(tmp_path):
    from hydraweave import version

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "hydraweave"\nversion = "2.4.0"\n', encoding="utf-8")
    with patch.object(version.subprocess, "run", side_effect=OSError):
        assert version.version_info(tmp_path) == ("2.4.0", None)


def test_release_tree_without_git_uses_release_json(tmp_path):
    import json
    from hydraweave import version

    (tmp_path / "RELEASE.json").write_text(json.dumps({"name": "hydraweave-x", "version": "2.4.0",
                                                         "commit": "0123456789abcdef"}), encoding="utf-8")
    with patch.object(version.metadata, "version", side_effect=version.metadata.PackageNotFoundError),             patch.object(version.subprocess, "run", side_effect=OSError):
        assert version.version_info(tmp_path) == ("2.4.0", "0123456789ab")


def test_unknown_portal_version_never_flags_nodes_outdated():
    from hydraweave.version import UNKNOWN, version_status

    assert version_status("0.1.0", UNKNOWN) == "unknown"


def test_versions_lock_check_warns_current_tensormeld_drift(capsys):
    from tools import check_versions_lock

    rows = check_versions_lock.mismatches()
    tensormeld = next((row for row in rows if row[0] == "tensormeld"), None)
    assert tensormeld is not None
    assert tensormeld[1] != tensormeld[2]
    assert check_versions_lock.main([]) == 0
    assert "::warning::versions.lock drift: tensormeld" in capsys.readouterr().out
    assert check_versions_lock.main(["--strict"]) == 1


def test_cli_parser_does_not_run_git_until_version_is_requested(capsys):
    import pytest
    from hydraweave import cli, version

    with patch.object(version.subprocess, "run", side_effect=AssertionError("git ran")):
        cli.build_parser()
    with patch.object(version.subprocess, "run", side_effect=OSError), pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--version"])
    assert capsys.readouterr().out.startswith("hydraweave ")
