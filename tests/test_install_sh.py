import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / "install.sh"
SOURCE = SCRIPT.read_text(encoding="utf-8").split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
SH = shutil.which("sh")


class InstallerHelpersTests(unittest.TestCase):
    def setUp(self):
        self.functions = {"__name__": "installer_test"}
        exec(compile(SOURCE, str(SCRIPT), "exec"), self.functions)

    def test_installer_embeds_the_shared_implementation_verbatim(self):
        shared = SCRIPT.parent / "hydraweave" / "releases.py"
        self.assertEqual(SOURCE, shared.read_text(encoding="utf-8").rstrip())

    def test_embedded_python_is_valid_and_rejects_unsafe_versions(self):
        compile(SOURCE, str(SCRIPT), "exec")
        for value in ("../../tmp", "v1/2", "v;echo", "v", "-1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.functions["version"](value)

    def test_missing_venv_prints_exact_manual_apt_command(self):
        with patch("importlib.util.find_spec", return_value=None), self.assertRaisesRegex(
                ValueError, "sudo apt-get install python3-venv"):
            self.functions["check_python"]()

    def test_controller_needs_311_but_node_accepts_310(self):
        with patch.object(sys, "version_info", (3, 10, 12)):
            with self.assertRaisesRegex(ValueError, "3.11 or newer"):
                self.functions["check_python"]()
            with patch("importlib.util.find_spec", return_value=object()):
                self.functions["check_python"](True)

    def test_duplicate_or_malformed_checksums_are_rejected(self):
        row = "0" * 64 + "  hydraweave-0.1.0-py3-none-any.whl\n"
        for data in ("bad", row + row, "0" * 64 + "  ../escape.whl\n"):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.functions["checksums"](data.encode())

    def test_no_release_explains_tagging_without_network(self):
        error = urllib.error.HTTPError("https://example.invalid", 404, "not found", {}, None)
        with patch.dict(self.functions, read_url=Mock(side_effect=error)), self.assertRaisesRegex(
                ValueError, "No matching GitHub release exists yet"):
            self.functions["release"](None)

    @unittest.skipUnless(SH, "requires sh")
    def test_sh_syntax(self):
        result = subprocess.run([SH, "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipUnless(os.name != "nt" and SH, "requires POSIX sh and POSIX venv layout")
class InstallShTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="hydraweave install '")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.release = self.root / "release"
        self.release.mkdir()
        self.wheel = self.release / "hydraweave-0.1.0-py3-none-any.whl"
        with zipfile.ZipFile(self.wheel, "w") as archive:
            archive.writestr("hydraweave/__init__.py", "")
            archive.writestr("hydraweave/cli.py", "import sys\ndef main():\n print(repr(sys.argv[1:]))\n")
            base = "hydraweave-0.1.0.dist-info/"
            archive.writestr(base + "METADATA", "Metadata-Version: 2.1\nName: hydraweave\nVersion: 0.1.0\nProvides-Extra: node\n")
            archive.writestr(base + "WHEEL", "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            archive.writestr(base + "entry_points.txt", "[console_scripts]\nhydraweave = hydraweave.cli:main\nhydraweave-node = hydraweave.cli:main\n")
            archive.writestr(base + "RECORD", "")
        self.sums = self.release / "SHA256SUMS"
        self.sums.write_text(hashlib.sha256(self.wheel.read_bytes()).hexdigest() + "  " + self.wheel.name + "\n")
        metadata = {"tag_name": "v0.1.0", "assets": [
            {"name": p.name, "browser_download_url": p.as_uri()} for p in (self.wheel, self.sums)]}
        api = self.root / "api"
        (api / "tags").mkdir(parents=True)
        (api / "latest").write_text(json.dumps(metadata))
        (api / "tags" / "v0.1.0").write_text(json.dumps(metadata))
        self.prefix = self.root / "prefix with ' spaces"
        self.env = dict(os.environ, HYDRAWEAVE_RELEASE_API=api.as_uri(), HOME=str(self.root),
                        PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_CONFIG_FILE=os.devnull)

    def run_installer(self, *args):
        return subprocess.run([SH, str(SCRIPT), "--prefix", str(self.prefix), *map(str, args)],
                              env=self.env, capture_output=True, text=True, timeout=60)

    def test_latest_dry_run_uses_metadata_without_downloading_or_writing(self):
        self.wheel.unlink()
        self.sums.unlink()
        result = self.run_installer("--dry-run", "--node")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("venvs/v0.1.0", result.stdout)
        self.assertIn("hydraweave-node", result.stdout)
        self.assertFalse(self.prefix.exists())

    def test_explicit_version_and_offline_dry_run(self):
        for flags in (("--version", "v0.1.0"), ("--from-file", self.wheel)):
            result = self.run_installer("--dry-run", *flags)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(self.prefix.exists())

    def test_tampered_wheel_and_missing_checksum_prevent_install(self):
        original = self.wheel.read_bytes()
        for tampered in (True, False):
            self.wheel.write_bytes(original + b"tampered" if tampered else original)
            if not tampered:
                self.sums.write_text("")
            result = self.run_installer("--from-file", self.wheel)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("SHA256", result.stderr)
            self.assertFalse(self.prefix.exists())

    def test_offline_install_and_shims_handle_spaces_quotes_and_arguments(self):
        result = self.run_installer("--from-file", self.wheel, "--node")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for command in ("hydraweave", "hydraweave-node"):
            shim = self.prefix / "bin" / command
            environment = dict(self.env)
            environment.pop("PYTHONPATH", None)
            run = subprocess.run([str(shim), "two words", "quote'"], capture_output=True, text=True,
                                 env=environment)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout.strip(), repr(["two words", "quote'"]))

    def test_online_install_from_local_release_assets(self):
        result = self.run_installer("--version", "v0.1.0")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.prefix / "bin" / "hydraweave").exists())
        self.assertFalse((self.prefix / "bin" / "hydraweave-node").exists())

    def test_offline_version_mismatch_and_missing_sums(self):
        result = self.run_installer("--from-file", self.wheel, "--version", "v9")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match", result.stderr)
        self.sums.unlink()
        result = self.run_installer("--from-file", self.wheel)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.prefix.exists())

