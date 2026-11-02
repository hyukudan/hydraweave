"""Public compatibility contracts retained across the project rename."""
from contextlib import redirect_stderr
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hydraweave import cli, compat, config, orchestrate, secrets


class RenameCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        patcher = patch.object(compat, "_NOTICES", set())
        patcher.start()
        self.addCleanup(patcher.stop)

    def child(self, *args):
        return subprocess.run([sys.executable, *args], capture_output=True,
                              text=True, timeout=30, check=True)

    def test_shim_warns_and_shares_nested_module_identity(self):
        result = self.child("-c", """
import importlib, warnings
with warnings.catch_warnings(record=True) as seen:
    warnings.simplefilter('always', DeprecationWarning)
    import hyperloom, hydraweave
    assert hyperloom.__version__ == hydraweave.__version__
    for suffix in ('config', 'cli', 'web.api', 'recipes.service'):
        assert importlib.import_module('hyperloom.' + suffix) is importlib.import_module('hydraweave.' + suffix)
    from hyperloom import config
    assert config is hydraweave.config
    assert any(issubclass(w.category, DeprecationWarning) for w in seen)
print('ok')
""")
        self.assertEqual(result.stdout.strip(), "ok")

    def test_old_and_new_module_entry_points_match(self):
        for args in (("--version",), ("--help",)):
            old = self.child("-m", "hyperloom", *args)
            new = self.child("-m", "hydraweave", *args)
            self.assertEqual(old.stdout, new.stdout)
            self.assertIn("hydraweave", new.stdout.lower())

    def test_distribution_declares_both_commands_and_packages(self):
        import tomllib
        data = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
        self.assertEqual(data["project"]["name"], "hydraweave")
        scripts = data["project"]["scripts"]
        self.assertEqual(scripts["hydraweave"], scripts["hyperloom"])
        self.assertIn("hyperloom*", data["tool"]["setuptools"]["packages"]["find"]["include"])

    def test_old_env_is_read_dynamically_with_one_value_free_notice(self):
        environment = {"HYPERLOOM_TELEMETRY": "private-value"}
        output = io.StringIO()
        with redirect_stderr(output):
            self.assertEqual(compat.getenv("HYDRAWEAVE_TELEMETRY", environ=environment), "private-value")
            environment["HYPERLOOM_TELEMETRY"] = "0"
            self.assertEqual(compat.getenv("HYDRAWEAVE_TELEMETRY", environ=environment), "0")
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertIn("deprecated", output.getvalue())
        self.assertNotIn("private-value", output.getvalue())

    def test_new_env_including_empty_value_wins_without_notice(self):
        for value in ("1", ""):
            output = io.StringIO()
            with redirect_stderr(output):
                self.assertEqual(compat.getenv("HYDRAWEAVE_TELEMETRY", "default", environ={
                    "HYDRAWEAVE_TELEMETRY": value, "HYPERLOOM_TELEMETRY": "0"}), value)
            self.assertEqual(output.getvalue(), "")
        self.assertEqual(compat.getenv("HYDRAWEAVE_TELEMETRY", "default", environ={}), "default")

    def test_recipe_core_reads_old_env_after_import(self):
        from hydraweave.recipes import core
        executable = self.root / ("core.exe" if os.name == "nt" else "core")
        executable.write_bytes(b"fixture")
        executable.chmod(0o700)
        with patch.dict(os.environ, {"HYPERLOOM_TS_RECIPE": str(executable)}, clear=True):
            self.assertEqual(core.locate(), str(executable))
            os.environ["HYDRAWEAVE_TS_RECIPE"] = ""
            self.assertIsNone(core.locate())

    def test_stub_reads_old_env(self):
        from hydraweave.recipes import core, launch
        with patch.object(core, "backend", return_value=SimpleNamespace(authoritative=False)), \
                patch.dict(os.environ, {"HYPERLOOM_RECIPE_STUB": "1"}, clear=True):
            self.assertFalse(launch._backend(None)[1])

    def test_config_fallback_and_precedence_on_both_platforms(self):
        for platform in ("nt", "posix"):
            base = self.root / "hydraweave" / platform
            environment = {"APPDATA": str(base)}
            parent = base if platform == "nt" else base / ".config"
            old = parent / "hyperloom" / "cluster.json"
            new = parent / "hydraweave" / "cluster.json"
            with patch.object(cli, "os", SimpleNamespace(name=platform, environ=environment)), \
                    patch.object(Path, "home", return_value=base):
                self.assertEqual(cli.config_path(), new)
                old.parent.mkdir(parents=True)
                old.write_text("legacy")
                new.parent.mkdir(parents=True)
                self.assertEqual(cli.config_path(), old)
                new.write_text("current")
                self.assertEqual(cli.config_path(), new)
                self.assertEqual(old.read_text(), "legacy")

    def test_runtime_fallback_keeps_existing_state_location(self):
        old = self.root / "hyperloom" / "run"
        new = self.root / "hydraweave" / "run"
        old.mkdir(parents=True)
        (old / "up-example.json").write_text("{}")
        platform = SimpleNamespace(name="nt", environ={"LOCALAPPDATA": str(self.root)})
        with patch.object(secrets, "os", platform):
            self.assertEqual(secrets.runtime_dir(), old)
            new.mkdir(parents=True)
            self.assertEqual(secrets.runtime_dir(), new)
            self.assertEqual(orchestrate._state_path("example"), old / "up-example.json")

    def test_state_list_merges_and_current_records_win(self):
        old = self.root / "hyperloom" / "run"
        new = self.root / "hydraweave" / "run"
        for directory in (old, new):
            directory.mkdir(parents=True)
        for directory, name in ((old, "up-old.json"), (old, "up-both.json"), (new, "up-both.json")):
            (directory / name).write_text("{}")
        with patch.object(secrets, "runtime_dir", return_value=new):
            self.assertEqual(set(orchestrate.launch_states(None, unknown=True)), {"old", "both"})
        paths = {p.name: p for p in compat.glob_paths(new, "up-*.json")}
        self.assertEqual(paths["up-both.json"].parent, new)
        self.assertEqual(compat.legacy_path(new / "hydraweave"), old / "hydraweave")
        self.assertEqual(compat.legacy_path(self.root / "unrelated-hydraweave" / "run"),
                         self.root / "unrelated-hydraweave" / "run")

    def test_saved_secret_falls_back_per_file_without_copying(self):
        old = self.root / "hyperloom" / "secrets"
        new = self.root / "hydraweave" / "secrets"
        old.mkdir(parents=True)
        new.mkdir(parents=True)
        (old / "example").write_bytes(b"a" * 64)
        with patch.object(secrets, "_directory", return_value=new), \
                patch.object(secrets, "_dpapi", side_effect=lambda data, **kw: data):
            self.assertEqual(secrets.get("example"), "a" * 64)
            self.assertFalse((new / "example").exists())
            (new / "example").write_bytes(b"b" * 64)
            self.assertEqual(secrets.get("example"), "b" * 64)
        self.assertEqual((old / "example").read_bytes(), b"a" * 64)

    def test_old_cluster_schema_is_accepted(self):
        data = json.loads((Path(__file__).parents[1] / "examples/cluster.example.json").read_text())
        data["schema"] = "hyperloom/cluster-v1"
        self.assertEqual(set(config.parse(data).machines), set(config.parse({**data, "schema": "hydraweave/cluster-v1"}).machines))

    def test_old_node_schema_and_poll_parts_are_normalized(self):
        from hydraweave import remote
        payload = {"schema": "hyperloom/node-v1", "parts": {"host": {"schema": "hyperloom/node-v1"}}}
        value = remote._json(json.dumps(payload).encode())
        self.assertEqual(value["schema"], "hydraweave/node-v1")
        self.assertEqual(value["parts"]["host"]["schema"], "hydraweave/node-v1")

    def test_legacy_api_path_and_response_envelope(self):
        self.assertEqual(compat.api_path("/hyperloom/api/status?x=1"), "/hydraweave/api/status?x=1")
        self.assertEqual(compat.api_path("/hyperloom-other/api/"), "/hyperloom-other/api/")
        document = {"schema": "hydraweave/ui-status-v1", "items": [{"schema": "hydraweave/node-v1"}], "name": "hydraweave"}
        converted = compat.legacy_document(document)
        self.assertEqual(converted["schema"], "hyperloom/ui-status-v1")
        self.assertEqual(converted["items"][0]["schema"], "hyperloom/node-v1")
        self.assertEqual(converted["name"], "hydraweave")
        self.assertEqual(document["schema"], "hydraweave/ui-status-v1")

    def test_legacy_markers_and_partial_downloads_use_same_root(self):
        sys.path.insert(0, str(Path(__file__).parents[1] / "node"))
        self.addCleanup(sys.path.pop, 0)
        node = importlib.import_module("hydraweave_compat")
        root = self.root / "hydraweave"
        root.mkdir()
        for suffix in ("-root", "-assets", "-part"):
            old = root / (".hyperloom" + suffix)
            new = root / (".hydraweave" + suffix)
            old.write_text("existing")
            self.assertEqual(node.read_marker(new), old)
            new.write_text("current")
            self.assertEqual(node.read_marker(new), new)

    def test_standalone_node_compatibility_is_in_bootstrap(self):
        from hydraweave import remote
        self.assertIn("hydraweave_compat.py", remote.NODE_FILES)

    def test_existing_service_unit_remains_addressable(self):
        sys.path.insert(0, str(Path(__file__).parents[1] / "node"))
        self.addCleanup(sys.path.pop, 0)
        service = importlib.import_module("hydraweave_service")
        with patch.object(service, "unit_dir", return_value=self.root):
            self.assertEqual(service.unit_name("example"), "hydraweave-example.service")
            (self.root / "hl-example.service").write_text("existing")
            self.assertEqual(service.unit_name("example"), "hl-example.service")
            (self.root / "hydraweave-example.service").write_text("current")
            self.assertEqual(service.unit_name("example"), "hydraweave-example.service")

    def test_old_bundle_ref_keeps_strict_pin_and_single_ref_checks(self):
        sys.path.insert(0, str(Path(__file__).parents[1] / "node"))
        self.addCleanup(sys.path.pop, 0)
        install = importlib.import_module("hydraweave_install")
        commit = "a" * 40
        for ref in ("refs/hyperloom/source", "refs/hydraweave/source"):
            self.assertEqual(install.source_ref({ref: commit}, commit), ref)
            with self.assertRaises(ValueError):
                install.source_ref({ref: "b" * 40}, commit)
            with self.assertRaises(ValueError):
                install.source_ref({ref: commit, "refs/heads/extra": commit}, commit)

    def test_router_state_fallback_with_a_new_config_and_runtime_directory(self):
        old = self.root / "hyperloom" / "run"
        new = self.root / "hydraweave" / "run"
        old.mkdir(parents=True)
        new.mkdir(parents=True)
        (old / "router-state.json").write_text("{}")
        with patch.object(cli, "config_path", return_value=new.parent / "cluster.json"):
            self.assertEqual(cli._runtime(), old)

    def test_asset_cache_reads_old_path_and_schema(self):
        from hydraweave import assets
        old = self.root / "hyperloom"
        new = self.root / "hydraweave"
        old.mkdir()
        new.mkdir()
        (old / "assets-manifest.json").write_text(json.dumps({"schema": "hyperloom/assets-manifest-v1", "assets": {}}))
        with patch.object(secrets, "_directory", return_value=new / "secrets"):
            self.assertEqual(assets.manifest_path(), old / "assets-manifest.json")
            self.assertEqual(assets.read_manifest(), {})
