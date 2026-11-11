"""First-run and role placement contracts; no network, GPU, or real configuration."""

from copy import deepcopy
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from hydraweave import cli, config
from hydraweave.web.api import API
from hydraweave.web.features import setup


class SetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "workspace" / "cluster.json"
        self.api = API(self.path, audit_path=self.root / "audit.jsonl")
        self.api.node = mock.Mock(side_effect=AssertionError("passive setup must not call a node"))
        self.state_path = mock.patch.object(setup.orchestrate, "_state_path", lambda name: self.root / ("up-" + name + ".json"))
        self.state_path.start()
        self.addCleanup(self.state_path.stop)

    def initialized(self):
        setup.initialize(self.api, {})
        return json.loads(self.path.read_text())

    def with_worker(self):
        raw = self.initialized()
        raw["machines"].append({"id": "halo-1", "os": "linux", "access": "ssh", "ssh_target": "halo-1",
                                "http_address": "192.168.1.10", "install_root": "/opt/hydraweave",
                                "lab_mode": True, "lab_dir": "/opt/lab"})
        raw["profiles"] = {"qwen": {"preset": "qwen-replicas", "nodes": ["halo-1"]}}
        setup.ops.write_config(self.path, raw)
        return raw

    def test_missing_workspace_is_actionable_without_writing(self):
        doc = setup.overview(self.api, {})
        self.assertFalse(doc["configured"])
        self.assertEqual(doc["configuration"]["state"], "missing")
        self.assertEqual(doc["next_action"]["action"], "setup/initialize")
        self.assertFalse(self.path.parent.exists())
        self.api.node.assert_not_called()

    def test_cli_update_check_failure_is_actionable_without_a_traceback(self):
        from hydraweave.web.api import JobFailed
        from hydraweave.web.features import components
        output = io.StringIO()
        with mock.patch.object(components, "check_validate", return_value={}), \
                mock.patch.object(components, "check", side_effect=JobFailed("upstream unavailable")), \
                mock.patch("hydraweave.cliext.control_center.API", return_value=self.api), \
                redirect_stderr(output):
            code = cli.main(["components", "check", "--config", str(self.path)])
        self.assertEqual(code, 1)
        self.assertIn("upstream unavailable", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())

    def test_initialize_creates_valid_controller_only_and_never_overwrites(self):
        raw = self.initialized()
        cluster = config.parse(raw)
        self.assertEqual(list(cluster.machines), ["controller"])
        self.assertEqual(cluster.profiles, {})
        self.assertEqual(cluster.assets, {})
        before = self.path.read_bytes()
        with self.assertRaises(PermissionError):
            setup.initialize(self.api, {})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(setup.overview(self.api, {})["next_action"]["id"], "nodes")

    def test_invalid_existing_file_stays_intact(self):
        self.path.parent.mkdir()
        self.path.write_text("broken config")
        self.assertEqual(setup.overview(self.api, {})["configuration"]["state"], "invalid")
        with self.assertRaises(PermissionError):
            setup.initialize(self.api, {})
        self.assertEqual(self.path.read_text(), "broken config")

    def test_configured_worker_is_not_claimed_reachable_installed_or_ready(self):
        self.with_worker()
        doc = setup.overview(self.api, {})
        node = next(row for row in doc["nodes"] if row["id"] == "halo-1")
        self.assertFalse(node["observed"])
        self.assertIsNone(node["state"])
        self.assertIsNone(doc["profiles"][0]["installed"])
        self.assertTrue(doc["profiles"][0]["missing_assets"])
        self.assertEqual(doc["lifecycle"]["mode"], "bounded-leases")
        self.api.node.assert_not_called()

    def test_cached_health_and_timestamp_are_preserved_without_new_probes(self):
        self.with_worker()
        self.api._cached = {"devices": {"machines": [{"id": "halo-1", "health": "online", "last_seen": 123.0}]}}
        node = next(row for row in setup.overview(self.api, {})["nodes"] if row["id"] == "halo-1")
        self.assertEqual(node["state"], "online")
        self.assertEqual(node["last_seen"], 123.0)
        self.assertTrue(node["observed"])
        self.api.node.assert_not_called()

    def test_router_preview_is_read_only_and_save_has_version_guard(self):
        self.with_worker()
        _, version = setup.ops._read_config(self.api)
        arguments = {"version": version, "machine": "halo-1", "port": 18611}
        before = self.path.read_bytes()
        preview = setup.router_validate(self.api, arguments)
        self.assertTrue(preview["valid"])
        self.assertEqual(preview["preview"]["listen"], "192.168.1.10:18611")
        self.assertEqual(self.path.read_bytes(), before)
        result = setup.router_save(self.api, arguments)
        self.assertTrue(result["saved"])
        self.assertEqual(config.load(self.path).router["machine"], "halo-1")
        self.assertEqual(self.path.with_name("cluster.json.prev").read_bytes(), before)
        with self.assertRaises(PermissionError):
            setup.router_save(self.api, arguments)

    def test_imported_remote_router_uses_its_node_address_with_the_default_port(self):
        raw = self.with_worker()
        raw["router"] = {"machine": "halo-1"}
        setup.ops.write_config(self.path, raw)
        self.assertEqual(setup.overview(self.api, {})["router"]["listen"], "192.168.1.10:18600")

    def test_router_change_refuses_saved_launch_or_unknown_target(self):
        self.with_worker()
        _, version = setup.ops._read_config(self.api)
        self.assertFalse(setup.router_validate(self.api, {"machine": "missing"})["valid"])
        (self.root / "up-qwen.json").write_text("even unreadable launch state must block")
        before = self.path.read_bytes()
        with self.assertRaises(PermissionError):
            setup.router_save(self.api, {"version": version, "machine": "halo-1"})
        self.assertEqual(self.path.read_bytes(), before)

    def test_router_respects_engine_local_only_constraint(self):
        raw = self.with_worker()
        second = deepcopy(raw["machines"][-1])
        second.update(id="halo-2", ssh_target="halo-2", http_address="192.168.1.11")
        raw["machines"].append(second)
        raw["profiles"] = {"mm": {"preset": "minimax-pair", "rank0": "halo-1", "rank1": "halo-2"}}
        setup.ops.write_config(self.path, raw)
        result = setup.router_validate(self.api, {"machine": "local"})
        self.assertFalse(result["valid"])
        self.assertIn("rank 0", result["error"])
        self.assertTrue(setup.router_validate(self.api, {"machine": "halo-1"})["valid"])

    def test_cli_and_portal_launch_use_saved_default_but_explicit_choice_wins(self):
        raw = self.with_worker()
        raw["router"].update(machine="halo-1", listen="192.168.1.10:18600")
        setup.ops.write_config(self.path, raw)
        args = cli.build_parser().parse_args(["up", "qwen", "--config", str(self.path)])
        cli._resolve_router(args)
        self.assertEqual(args.router, "halo-1")
        explicit = cli.build_parser().parse_args(["up", "qwen", "--router", "local", "--config", str(self.path)])
        cli._resolve_router(explicit)
        self.assertEqual(explicit.router, "local")
        request = self.api._arguments("up", {"profile": "qwen"})
        self.assertEqual(request["router"], "halo-1")
        self.assertEqual(self.api._arguments("up", {"profile": "qwen", "router": "local"})["router"], "local")

    def test_new_cli_commands_and_ui_accept_explicit_workspace(self):
        for command in (["setup", "init"], ["setup", "status"], ["components", "status"],
                        ["components", "check"], ["components", "plan"]):
            args = cli.build_parser().parse_args(command + ["--config", str(self.path)])
            self.assertEqual(args.config, self.path)
            self.assertTrue(callable(args.ext_run))
        args = cli.build_parser().parse_args(["ui", "--config", str(self.path), "--no-browser"])
        self.assertEqual(args.config, self.path)


if __name__ == "__main__":
    unittest.main()
