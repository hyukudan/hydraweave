import builtins
import importlib.util
import json
from pathlib import Path
import runpy
from unittest.mock import patch

try:
    import pytest
except ImportError as exc:  # stdlib unittest discovery runs before test extras
    import unittest

    raise unittest.SkipTest("pytest is required") from exc


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("package_builder", ROOT / "packaging/build.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def test_deb_recipe_runtime_dependencies_and_optional_build_tools(tmp_path):
    args = builder.command("deb", tmp_path, tmp_path / "node.deb")
    depends = [args[index + 1] for index, value in enumerate(args[:-1]) if value == "--depends"]
    recommends = [args[index + 1] for index, value in enumerate(args[:-1]) if value == "--deb-recommends"]
    assert "python3 >= 3.10" in depends and "python3-tomli" in depends
    assert not any("cmake" in item or "ninja" in item for item in depends)
    assert recommends == ["cmake", "ninja-build"]


@pytest.mark.parametrize("kind", ["deb", "rpm"])
def test_package_dry_run_stages_without_fpm_or_network(tmp_path, kind):
    with patch.object(builder.subprocess, "run") as run:
        result = builder.build(kind, tmp_path, dry_run=True)
    run.assert_not_called()
    assert result["dry_run"]
    stage = Path(result["stage"])
    assert (stage / "usr/lib/hydraweave/hydraweave/node_agent.py").is_file()
    assert (stage / "usr/lib/hydraweave/hydraweave/uninstall.py").is_file()
    assert (stage / "usr/lib/systemd/user/hydraweave-node.service").is_file()
    assert not any(path.name == "__pycache__" for path in stage.rglob("*"))


def test_build_cli_defaults_to_deb_for_dry_run(tmp_path, capsys):
    with patch("sys.argv", ["build.py", "--dry-run", "--output", str(tmp_path)]), \
            patch.object(builder.subprocess, "run") as run:
        builder.main()
    assert json.loads(capsys.readouterr().out)["command"][4] == "deb"
    run.assert_not_called()


def test_python310_without_tomli_imports_and_supports_json():
    original = builtins.__import__

    def without_toml(name, *args, **kwargs):
        if name in {"tomllib", "tomli"}:
            raise ImportError(name)
        return original(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=without_toml):
        namespace = runpy.run_path(str(ROOT / "hydraweave/recipes/reader.py"))
    assert namespace["tomllib"] is None
    # Exercise the parser, not only the optional import, with the module's own globals.
    parse = namespace["parse_set"]
    assert parse("limits.count=2") == ("limits.count", 2)
    assert parse("enabled=false") == ("enabled", False)
    assert parse('label="node1"') == ("label", "node1")
    assert parse("label=node1") == ("label", "node1")
    reader = namespace["load_text"]
    assert reader('{"schema":"test"}', "inline") == {"schema": "test"}
    with pytest.raises(namespace["RecipeError"], match="use a JSON recipe"):
        reader('schema = "test"', "inline")


def test_python310_uses_installed_tomli():
    from hydraweave.recipes import reader
    if reader.tomllib is None:
        pytest.skip("TOML parser not installed")
    original = builtins.__import__

    def only_tomli(name, *args, **kwargs):
        if name == "tomllib":
            raise ImportError(name)
        if name == "tomli":
            return reader.tomllib
        return original(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=only_tomli):
        namespace = runpy.run_path(str(ROOT / "hydraweave/recipes/reader.py"))
    assert namespace["parse_set"]("enabled=true") == ("enabled", True)
