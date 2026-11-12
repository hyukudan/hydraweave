"""Authenticated node updates and discovery version hints without hardware."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from hydraweave import __version__, bootstrap, discovery, node_agent, onboarding
from hydraweave.web.features import discovery as web


def agent_at(tmp_path, *, leases=(), turns=None, data=b"package"):
    runner = Mock(return_value=SimpleNamespace(returncode=0))
    agent = node_agent.Agent(("127.0.0.1", 0), tmp_path / "node", record={"address": "127.0.0.1"},
                             lab_dir=str(tmp_path / "lab") if turns is not None else None,
                             runner=runner, download=Mock(return_value=data),
                             call=Mock(return_value=turns))
    python = agent.root / "agent-venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    tool = SimpleNamespace(extension_for=lambda _: SimpleNamespace(active_leases=lambda _: leases))
    payload = {"origin": "http://192.0.2.1:18700", "sha256": hashlib.sha256(data).hexdigest()}
    return agent, tool, payload


def test_update_refused_during_live_lease_before_download_or_commands(tmp_path):
    agent, tool, payload = agent_at(tmp_path, leases=[{"lease": "live"}])
    try:
        with patch.object(node_agent, "load_tool", return_value=tool), \
                pytest.raises(ValueError, match="live GPU lease"):
            agent.invoke("agent-update", payload)
        agent.runner.assert_not_called()
        agent.download.assert_not_called()
        assert not agent.updating
    finally:
        agent.server_close()


@pytest.mark.parametrize("turns", [{"present": True, "holder": {"alive": True}},
                                   {"present": True, "holder": {"alive": None}},
                                   {"present": False}, {"error": "unreadable"}])
def test_update_refused_during_turn_or_unknown_state(tmp_path, turns):
    agent, tool, payload = agent_at(tmp_path, turns=turns)
    try:
        with patch.object(node_agent, "load_tool", return_value=tool), pytest.raises(ValueError, match="turn"):
            agent.invoke("agent-update", payload)
        agent.runner.assert_not_called()
        agent.download.assert_not_called()
    finally:
        agent.server_close()


def test_update_repulls_verified_package_and_schedules_user_restart(tmp_path):
    agent, tool, payload = agent_at(tmp_path, turns={"present": True, "holder": None})
    try:
        with patch.object(node_agent, "load_tool", return_value=tool):
            result = agent.invoke("agent-update", payload)
        assert result == {"state": "updated", "restart": "scheduled"}
        agent.download.assert_called_once_with(payload["origin"] + "/node/package.tar.gz")
        commands = [call.args[0] for call in agent.runner.call_args_list]
        assert commands[0] == ["systemctl", "--user", "is-active", "hydraweave-node.service"]
        assert "pip" in commands[1] and "package.tar.gz" in commands[1][-1]
        assert commands[2][0] == "systemd-run" and "--on-active=2s" in commands[2]
        assert commands[2][-4:] == ["systemctl", "--user", "restart", "hydraweave-node.service"]
        assert not any("sudo" in arg for command in commands for arg in command)
        with pytest.raises(ValueError, match="new work is refused"):
            agent.invoke("lease", {})
    finally:
        agent.server_close()


def test_bad_package_hash_never_reaches_pip(tmp_path):
    agent, tool, payload = agent_at(tmp_path)
    payload["sha256"] = "0" * 64
    try:
        with patch.object(node_agent, "load_tool", return_value=tool), pytest.raises(ValueError, match="SHA256"):
            agent.invoke("agent-update", payload)
        assert agent.runner.call_count == 1  # only service availability
        assert not agent.updating
    finally:
        agent.server_close()


def test_agent_update_requires_existing_bearer_authentication(tmp_path):
    from http.client import HTTPConnection
    import threading
    agent, tool, payload = agent_at(tmp_path)
    thread = threading.Thread(target=agent.serve_forever, daemon=True)
    thread.start()
    try:
        # AgentHandler decodes JSON with the normal tool; invoke must never be reached.
        with patch.object(agent, "invoke") as invoke:
            connection = HTTPConnection("127.0.0.1", agent.server_port)
            connection.request("POST", "/call", json.dumps({"verb": "agent-update", "payload": payload}))
            response = connection.getresponse()
            assert response.status == 403
            response.read()
            connection.close()
            invoke.assert_not_called()
    finally:
        agent.shutdown()
        agent.server_close()
        thread.join()


def test_discovery_compares_versions_on_every_poll_and_commit_if_known():
    registry = discovery.Registry()
    with patch.object(discovery, "controller_commit", return_value=""):
        registry.update({"id": "node1", "agent_version": "0.0.0"})
        assert registry.rows()[0]["version_mismatch"]
        registry.update({"id": "node1", "agent_version": __version__})
        assert not registry.rows()[0]["version_mismatch"]
    with patch.object(discovery, "controller_commit", return_value="a" * 40):
        assert registry.rows()[0]["version_mismatch"]
        registry.update({"id": "node1", "agent_version": __version__, "agent_commit": "a" * 40})
        assert not registry.rows()[0]["version_mismatch"]


def test_controller_job_sends_origin_and_verified_package_digest(tmp_path):
    machine = SimpleNamespace(id="node1", access="agent")
    api = SimpleNamespace(config_path=tmp_path / "cluster.json", cluster=lambda: SimpleNamespace(machines={"node1": machine}))
    ctx = Mock()
    ctx.cancelled.return_value = False
    arguments = {"machine": "node1", "origin": "http://192.0.2.1:18700"}
    client = Mock()
    client.call.return_value = {"state": "updated"}
    with patch.object(onboarding, "credentials", return_value={"node1": {"token": "paired"}}), \
            patch.object(onboarding, "Client", return_value=client), \
            patch.object(bootstrap, "source_package", return_value=b"package"):
        result = web.update_node(api, arguments, ctx)
    assert result["state"] == "updated"
    client.call.assert_called_once_with("agent-update", {"origin": arguments["origin"],
                                        "sha256": hashlib.sha256(b"package").hexdigest()}, timeout=1900)


def test_node_update_job_refuses_unpaired_and_browser_commands(tmp_path):
    machine = SimpleNamespace(id="node1", access="agent")
    api = SimpleNamespace(config_path=tmp_path / "cluster.json", cluster=lambda: SimpleNamespace(machines={"node1": machine}))
    with patch.object(onboarding, "credentials", return_value={}):
        with pytest.raises(ValueError, match="paired"):
            web.update_validate(api, {"machine": "node1", "origin": "http://192.0.2.1"})
    with pytest.raises(ValueError):
        web.update_validate(api, {"machine": "node1", "command": "evil"})


def test_controller_packages_have_stable_digest_across_seconds():
    with patch("gzip.time.time", side_effect=[1000, 2000]):
        assert hashlib.sha256(bootstrap.source_package()).digest() == hashlib.sha256(bootstrap.source_package()).digest()
