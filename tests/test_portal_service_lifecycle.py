"""Portal service lifecycle with filesystem fixtures and no systemd calls."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from hydraweave import node_agent


class PortalServiceLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.unit = self.home / ".config/systemd/user/hydraweave.service"
        for change in (patch.object(Path, "home", return_value=self.home),
                       patch.object(node_agent.sys, "platform", "linux")):
            change.start()
            self.addCleanup(change.stop)

    def symlink(self, link, target, *, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except OSError:
            self.skipTest("symlinks unavailable on this host")

    def test_portal_keeps_venv_interpreter_path_instead_of_resolving_to_base_python(self):
        base = self.home / "base-python"
        base.touch()
        environment = self.home / "venv with spaces"
        executable = environment / "bin/python"
        executable.parent.mkdir(parents=True)
        self.symlink(executable, base)
        with patch.object(node_agent.sys, "executable", str(executable)), \
                patch.object(node_agent.sys, "prefix", str(environment)):
            node_agent.service_command("install", run=Mock())
        self.assertIn(f'ExecStart="{executable.as_posix()}" -m hydraweave', self.unit.read_text())
        self.assertNotIn(f'ExecStart="{base.as_posix()}"', self.unit.read_text())

    def test_managed_portal_service_follows_current_release(self):
        root = self.home / "prefix/share/hydraweave"
        environment = root / "venvs/v1"
        executable = environment / "bin/python"
        executable.parent.mkdir(parents=True)
        executable.touch()
        self.symlink(root / "current", Path("venvs/v1"), directory=True)
        with patch.object(node_agent.sys, "executable", str(executable)), \
                patch.object(node_agent.sys, "prefix", str(environment)):
            node_agent.service_command("install", run=Mock())
        self.assertIn(f'ExecStart="{(root / "current/bin/python").as_posix()}"', self.unit.read_text())
        second = root / "venvs/v2/bin/python"
        second.parent.mkdir(parents=True)
        second.touch()
        (root / "current").unlink()
        self.symlink(root / "current", Path("venvs/v2"), directory=True)
        self.assertEqual((root / "current/bin/python").resolve(), second)

    def test_remove_stops_existing_unit_before_unlink_and_reload(self):
        self.unit.parent.mkdir(parents=True)
        self.unit.write_text("fixture")
        observed = []

        def run(argv, **kwargs):
            observed.append((argv[2], self.unit.exists()))
            return subprocess.CompletedProcess(argv, 0)

        node_agent.service_command("remove", run=run)
        self.assertEqual(observed, [("disable", True), ("daemon-reload", False)])

    def test_managed_agent_unit_and_path_follow_current_release(self):
        root = self.home / "prefix/share/hydraweave"
        environment = root / "venvs/v1"
        executable = environment / "bin/python"
        executable.parent.mkdir(parents=True)
        executable.touch()
        agent_name = "hydraweave-node" + (".exe" if os.name == "nt" else "")
        (executable.parent / agent_name).touch()
        self.symlink(root / "current", Path("venvs/v1"), directory=True)
        with patch.object(node_agent.sys, "executable", str(executable)), \
                patch.object(node_agent.sys, "prefix", str(environment)):
            node_agent.install_service(self.home / "node", run=Mock())
        text = self.unit.with_name("hydraweave-node.service").read_text()
        self.assertIn(f'ExecStart="{(root / "current/bin" / agent_name).as_posix()}"', text)
        self.assertIn(f'Environment="PATH={(root / "current/bin").as_posix()}:', text)

    def test_failed_stop_keeps_unit_for_retry(self):
        self.unit.parent.mkdir(parents=True)
        self.unit.write_text("fixture")
        runner = Mock(side_effect=subprocess.CalledProcessError(1, ["systemctl"]))
        with self.assertRaises(subprocess.CalledProcessError):
            node_agent.service_command("remove", run=runner)
        self.assertEqual(self.unit.read_text(), "fixture")
        self.assertEqual(runner.call_count, 1)

    def test_unit_interpreter_rejects_systemd_expansion_before_writing(self):
        for directory in ("bad%h", "bad${HOME}"):
            runner = Mock()
            with self.subTest(directory=directory), \
                    patch.object(node_agent.sys, "executable", str(self.home / directory / "bin/python")):
                with self.assertRaisesRegex(ValueError, "systemd path"):
                    node_agent.service_command("install", run=runner)
            self.assertFalse(self.unit.exists())
            runner.assert_not_called()

    def test_agent_root_rejects_systemd_environment_expansion(self):
        runner = Mock()
        with self.assertRaisesRegex(ValueError, "systemd path"):
            node_agent.install_service(self.home / "node${USER}", run=runner)
        self.assertFalse(self.unit.exists())
        self.assertFalse(self.unit.with_name("hydraweave-node.service").exists())
        self.assertEqual(runner.call_count, 1)  # Read-only is-active; never enable an invalid unit.
        self.assertEqual(runner.call_args.args[0][2], "is-active")


if __name__ == "__main__":
    unittest.main()
