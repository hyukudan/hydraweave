from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hydraweave import paths


class RepositoryPathsTests(unittest.TestCase):
    def test_xdg_defaults_overrides_and_windows(self):
        home = Path.cwd() / "test-home"
        for kind, suffix in (("config", ".config"), ("state", ".local/state"), ("data", ".local/share")):
            self.assertEqual(paths.user_dir(kind, home=home, environ={}, platform="posix"),
                             home / suffix / "hydraweave")
            custom = str(home / "custom")
            self.assertEqual(paths.user_dir(kind, home=home, environ={"XDG_" + kind.upper() + "_HOME": custom},
                                            platform="posix"), Path(custom) / "hydraweave")
            self.assertEqual(paths.user_dir(kind, home=home, environ={"HYDRAWEAVE_HOME": custom}, platform="nt"),
                             Path(custom) / kind)
        env = {"APPDATA": str(home / "roaming"), "LOCALAPPDATA": str(home / "local")}
        self.assertEqual(paths.config_dir(home=home, environ=env, platform="nt"), home / "roaming/hydraweave")
        self.assertEqual(paths.state_dir(home=home, environ=env, platform="nt"), home / "local/hydraweave/state")
        self.assertEqual(paths.data_dir(home=home, environ=env, platform="nt"), home / "local/hydraweave")

    def test_relative_xdg_is_ignored(self):
        home = Path.cwd() / "test-home"
        self.assertEqual(paths.config_dir(home=home, environ={"XDG_CONFIG_HOME": "relative"}, platform="posix"),
                         home / ".config/hydraweave")

    def test_new_runtime_and_existing_workspace_compatibility(self):
        from hydraweave import cli, secrets
        from hydraweave.recipes import service
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            env = {"HYDRAWEAVE_HOME": str(home / "new"), "LOCALAPPDATA": str(home / "local")}
            with patch.dict(paths.os.environ, env, clear=True), patch.object(paths.Path, "home", return_value=home):
                self.assertEqual(paths.runtime_dir(), home / "new/state/run")
                self.assertEqual(cli._runtime(), secrets.runtime_dir())
                with service.configured(home / "new/config/cluster.json"):
                    self.assertEqual(service.runtime_dir(), secrets.runtime_dir())
                old = (home / "local/hydraweave/run" if paths.os.name == "nt" else home / ".config/hydraweave/run")
                old.mkdir(parents=True)
                (old / "saved.json").write_text("{}")
                self.assertEqual(paths.runtime_dir(), old)
                self.assertTrue((old / "saved.json").exists())
                custom = home / "workspace/cluster.json"
                self.assertEqual(paths.runtime_dir(config=custom), custom.parent / "run")

    def test_config_and_secret_read_fallback_but_new_writes_use_override(self):
        from hydraweave import cli, secrets
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            env = {"HYDRAWEAVE_HOME": str(home / "new"), "APPDATA": str(home / "roaming")}
            with patch.dict(paths.os.environ, env, clear=True), patch.object(paths.Path, "home", return_value=home), \
                    patch.object(secrets, "_dpapi", side_effect=lambda data, decrypt=False: data):
                old = paths.legacy_config_dir()
                (old / "secrets").mkdir(parents=True)
                (old / "cluster.json").write_text("{}")
                (old / "secrets/model1").write_text("a" * 64)
                self.assertEqual(cli.config_path(), old / "cluster.json")
                self.assertEqual(secrets.get("model1"), "a" * 64)
                value = secrets.rotate("model1")
                self.assertTrue((home / "new/config/.hydraweave-root").is_file())
                self.assertEqual(secrets.get("model1"), value)
                self.assertEqual((old / "secrets/model1").read_text(), "a" * 64)
                new = home / "new/config/cluster.json"
                new.write_text("{}")
                self.assertEqual(cli.config_path(), new)

    def test_only_new_user_roots_receive_markers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            existing = root / "existing"
            existing.mkdir()
            paths.ensure_owned(existing)
            self.assertFalse((existing / ".hydraweave-root").exists())
            paths.ensure_owned(root / "new")
            self.assertTrue((root / "new/.hydraweave-root").is_file())

    def test_checkout_root_is_package_parent(self):
        self.assertEqual(paths.repo_root(), Path(paths.__file__).resolve().parent.parent)

    def test_incomplete_install_explains_editable_requirement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(paths, "_ROOT", root):
                with self.assertRaisesRegex(RuntimeError, r"node, versions.lock.*pip install -e"):
                    paths.repo_root()
                (root / "node").mkdir()
                with self.assertRaisesRegex(RuntimeError, "versions.lock"):
                    paths.repo_root()
                (root / "versions.lock").touch()
                self.assertEqual(paths.repo_root(), root)
