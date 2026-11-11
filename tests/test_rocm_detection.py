import importlib.util
import os
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("rocm_under_test", ROOT / "node" / "hydraweave_rocm.py")
rocm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rocm)


class RocmDetectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def directory(self, name):
        path = self.root / "opt" / name
        path.mkdir(parents=True)
        return path

    def test_explicit_root_wins(self):
        override = self.directory("custom-sdk")
        self.directory("rocm-99.0")
        self.assertEqual(rocm.detect_rocm({"ROCM_PATH": str(override)}), override)

    def test_standard_root_precedes_versioned_install(self):
        expected = self.directory("rocm")
        self.directory("rocm-99.0")
        self.assertEqual(rocm.detect_rocm({}, self.root), expected)

    @unittest.skipIf(os.name == "nt", "directory symlinks require Windows privileges")
    def test_standard_symlink_precedes_newer_sdk(self):
        target = self.directory("rocm-6.4")
        standard = self.root / "opt" / "rocm"
        standard.symlink_to(target, target_is_directory=True)
        self.directory("rocm-7.0")
        self.assertEqual(rocm.detect_rocm({}, self.root), standard)

    def test_numeric_version_sort_and_legacy_core_layout(self):
        self.directory("rocm-6.9")
        self.directory("rocm-6.10")
        self.directory("rocm/core-9.9")
        expected = self.directory("rocm/core-10.1")
        self.assertEqual(rocm.detect_rocm({}, self.root), expected)
        newest = self.directory("rocm-11.0")
        self.assertEqual(rocm.detect_rocm({}, self.root), newest)

    def test_invalid_override_falls_back_and_files_are_ignored(self):
        expected = self.directory("rocm-6.10")
        (self.root / "opt" / "rocm-99").write_text("not an SDK")
        self.assertEqual(rocm.detect_rocm({"ROCM_PATH": str(self.root / "missing")}, self.root), expected)

    def test_no_installation_returns_none_without_host_access(self):
        self.assertIsNone(rocm.detect_rocm({}, self.root))

    def test_runtime_libraries_use_only_the_selected_sdk(self):
        sdk = self.directory("rocm-7")
        env = rocm.runtime_environment(sdk)
        self.assertEqual(env, {"LD_LIBRARY_PATH": f"{sdk / 'lib'}:{sdk / 'lib64'}"})
