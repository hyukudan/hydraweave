"""The portal's plain Start requires a declared lab manager before any side effect."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest import mock

from hydraweave import orchestrate
from hydraweave.recipes import launch
from hydraweave.web.api import API
from tests.test_orchestrate import cluster_fixture


class LaunchPrerequisiteTests(unittest.TestCase):
    def test_plain_up_refuses_nonlab_before_state_or_ssh(self):
        for pair in (False, True):
            cluster = cluster_fixture(pair=pair)
            profile = "ds41-pair" if pair else "qwen-replicas"
            cluster.machines["halo-1"] = replace(cluster.machines["halo-1"], lab_mode=False)
            with self.subTest(pair=pair), \
                    mock.patch.object(orchestrate, "_state_path", side_effect=AssertionError("state access")), \
                    mock.patch.object(orchestrate, "_client", side_effect=AssertionError("SSH access")), \
                    self.assertRaisesRegex(orchestrate.OrchestrationError, "lab_mode is false") as error:
                orchestrate.up(cluster, profile)
            self.assertIn("leases only" if pair else "service install/start", str(error.exception))

    def test_missing_lab_directory_and_pair_alias_are_early_errors(self):
        cluster = cluster_fixture(pair=True)
        cluster.raw["machines"][1].pop("lab_dir")
        with self.assertRaisesRegex(orchestrate.OrchestrationError, "halo-2: lab_mode requires"):
            orchestrate.check_launch(cluster, "ds41-pair")
        cluster.raw["machines"][1]["lab_dir"] = "/opt/lab"
        cluster.raw["machines"][0]["peer_ssh"] = {}
        with self.assertRaisesRegex(orchestrate.OrchestrationError, "missing node-side peer_ssh"):
            orchestrate.check_launch(cluster, "ds41-pair")

    def test_router_host_does_not_need_a_gpu_turn_manager(self):
        cluster = cluster_fixture()
        cluster.machines["router"] = replace(cluster.machines["halo-1"], id="router", lab_mode=False)
        orchestrate.check_launch(cluster, "qwen-replicas", router="router")

    def test_rtx_only_recipe_keeps_its_existing_placement_path(self):
        cluster = cluster_fixture()
        cluster.machines["halo-1"] = replace(cluster.machines["halo-1"], lab_mode=False)
        prepared = SimpleNamespace(placement_request=object())
        with mock.patch.object(launch, "prepare", return_value=prepared), \
                mock.patch.object(launch, "check_admission"), \
                mock.patch.object(launch, "up_placement", return_value={"placement": True}) as placed, \
                mock.patch.object(orchestrate, "_client", side_effect=AssertionError("SSH access")):
            self.assertEqual(orchestrate.up(cluster, "qwen-replicas", recipe=object()), {"placement": True})
        placed.assert_called_once()

    def test_remote_recipe_checks_lab_before_publishing_state(self):
        cluster = cluster_fixture()
        cluster.machines["halo-1"] = replace(cluster.machines["halo-1"], lab_mode=False)
        prepared = SimpleNamespace(placement_request=None, service_start=False)
        with mock.patch.object(launch, "prepare", return_value=prepared), \
                mock.patch.object(launch, "check_admission"), \
                mock.patch.object(orchestrate, "_write", side_effect=AssertionError("state write")), \
                mock.patch.object(orchestrate, "_client", side_effect=AssertionError("SSH access")), \
                self.assertRaisesRegex(orchestrate.OrchestrationError, "lab_mode is false"):
            orchestrate.up(cluster, "qwen-replicas", recipe=object())

    def test_profile_doc_reports_start_blocker_without_blocking_install(self):
        cluster = cluster_fixture()
        cluster.machines["halo-1"] = replace(cluster.machines["halo-1"], lab_mode=False)
        api = API("/missing/cluster.json")
        with mock.patch.object(api, "_action_log", return_value=[]), \
                mock.patch.object(orchestrate, "_client", side_effect=AssertionError("SSH access")):
            row = api._profiles(cluster, {})[0]
        self.assertIn("lab_mode is false", row["launch_blocker"])
        self.assertNotIn("blocker", row)
        self.assertIsNot(row.get("available"), False)


if __name__ == "__main__":
    unittest.main()
