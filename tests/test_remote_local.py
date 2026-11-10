"""A Linux controller reaches its own host's node tool as a local child, with the commands ssh would run."""
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

from hydraweave import remote
from hydraweave.config import ConfigError, parse


def machine(**changes):
    values = dict(id="halo-1", os="linux", access="ssh", http_address="192.0.2.10", ssh_target="op@halo-1",
                  ssh_port=22, install_root="/home/op/hydraweave", lab_mode=True, devices=[], lab_dir=None,
                  peer_ssh={}, local=None)
    values.update(changes)
    return types.SimpleNamespace(**values)


class SelfDetectionTests(unittest.TestCase):
    def test_explicit_flag_decides(self):
        with mock.patch.object(remote.os, "name", "posix"), \
                mock.patch.object(remote, "_address_is_local", return_value=True):
            self.assertTrue(remote.is_self(machine(local=True, http_address="198.51.100.1")))
            self.assertFalse(remote.is_self(machine(local=False)))

    def test_detection_needs_an_owned_address_and_the_same_user(self):
        with mock.patch.object(remote.os, "name", "posix"), \
                mock.patch.object(remote, "_current_user", return_value="op"):
            with mock.patch.object(remote, "_address_is_local", return_value=True):
                self.assertTrue(remote.is_self(machine()))
                self.assertTrue(remote.is_self(machine(ssh_target="halo-1")))
                self.assertFalse(remote.is_self(machine(ssh_target="other@halo-1")))
                self.assertFalse(remote.is_self(machine(http_address="127.0.0.1")))   # loopback names every host
                self.assertFalse(remote.is_self(machine(os="windows")))
            with mock.patch.object(remote, "_address_is_local", return_value=False):
                self.assertFalse(remote.is_self(machine()))

    def test_never_local_off_posix(self):
        with mock.patch.object(remote.os, "name", "nt"):
            self.assertFalse(remote.is_self(machine(local=True)))

    def test_a_documentation_address_is_not_ours(self):
        self.assertFalse(remote._address_is_local("192.0.2.123"))
        self.assertTrue(remote._address_is_local("127.0.0.1"))


class TransportTests(unittest.TestCase):
    def test_local_mode_runs_the_same_command_through_sh_without_ssh_or_host_keys(self):
        known = mock.Mock()
        client = remote.Remote.__new__(remote.Remote)
        client.machine, client.known_hosts, client.ssh, client.local = machine(), known, "ssh", True
        with mock.patch.object(remote, "_run", return_value=b"ok\n") as run:
            self.assertEqual(client._ssh("printf ok", b"in", timeout=5), b"ok\n")
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["/bin/sh", "-c", "printf ok"])
        self.assertEqual(run.call_args.args[1], b"in")
        self.assertNotIn("SSH_AUTH_SOCK", run.call_args.kwargs["env"])
        known.options.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "the local node tool path runs on Linux controllers")
    def test_bootstrap_and_version_match_the_node_tool_run_directly(self):
        with tempfile.TemporaryDirectory() as home:
            env = {"HOME": home, "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
            with mock.patch.dict(os.environ, env), mock.patch.object(remote, "_home", return_value=home):
                client = remote.Remote(machine(local=True), Path(home) / "known_hosts")
                self.assertTrue(client.local)
                reply = client.bootstrap()
                tool = Path(home) / ".hydraweave-boot" / "hydraweave-node"
                self.assertTrue(tool.is_file())
                self.assertEqual(reply.get("verb"), "version")
                again = client.call("version", {})
                self.assertEqual(json.dumps(again, sort_keys=True), json.dumps(reply, sort_keys=True))


class ConfigTests(unittest.TestCase):
    def test_local_flag_is_validated(self):
        from tests.test_config import example
        obj = example()
        target = next(m for m in obj["machines"] if m["access"] == "ssh")
        target["local"] = True
        self.assertTrue(parse(obj).machines[target["id"]].local)
        target["local"] = "yes"
        with self.assertRaises(ConfigError):
            parse(obj)
        obj = example()
        windows = next(m for m in obj["machines"] if m["os"] == "windows")
        windows["local"] = True
        with self.assertRaises(ConfigError):
            parse(obj)


if __name__ == "__main__":
    unittest.main()


class LegacyToolTests(unittest.TestCase):
    def test_nodes_update_bootstraps_past_a_pre_rename_tool_without_asking_it_for_leases(self):
        from hydraweave import orchestrate
        client = mock.Mock()
        client.has_tool.side_effect = lambda *a, legacy=True, **k: legacy   # only ~/.hyperloom-boot exists
        cluster = types.SimpleNamespace(machines={"halo-2": machine(id="halo-2")})
        with mock.patch.object(orchestrate, "_profile", return_value=(None, None, ["halo-2"])), \
                mock.patch.object(orchestrate, "_client", return_value=client), \
                mock.patch.object(orchestrate, "_tool", return_value="digest"), \
                mock.patch.object(orchestrate, "_call") as call:
            result = orchestrate.nodes_update(cluster, "qwen-replicas")
        call.assert_not_called()
        client.bootstrap.assert_called_once()
        self.assertIn("halo-2", result)
