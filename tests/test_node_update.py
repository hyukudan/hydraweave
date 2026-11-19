"""Authenticated node updates and discovery version hints without hardware."""
import hashlib
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

try:
    import pytest
except ImportError:  # pragma: no cover - unittest discovery without the test extra
    from unittest import SkipTest
    raise SkipTest("requires the test extra (pytest)") from None

from hydraweave import __version__, admission, bootstrap, discovery, node_agent, node_update, onboarding
from hydraweave.web import features
from hydraweave.web.api import API, APIError
from hydraweave.web.features import discovery as web


def agent_at(tmp_path, *, leases=(), turns=None, data=b"package"):
    node_agent.load_tool()  # Make the packaged node helpers available, as production dispatch does.
    def command(argv, **kwargs):
        stdout = ""
        if argv[1:3] == ["-m", "venv"]:
            python = Path(argv[-1]) / "bin/python"
            python.parent.mkdir()
            python.touch()
        if "-c" in argv:
            prefix = str(Path(argv[0]).parent.parent)
            stdout = json.dumps({"version": __version__, "commit": "", "prefix": prefix,
                                 "module": prefix + "/lib/hydraweave/node_agent.py"})
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
    runner = Mock(side_effect=command)
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


@pytest.mark.skipif(sys.platform != "linux", reason="Linux user-service update")
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
@pytest.mark.skipif(sys.platform != "linux", reason="Linux user-service update")
def test_update_refused_during_turn_or_unknown_state(tmp_path, turns):
    agent, tool, payload = agent_at(tmp_path, turns=turns)
    try:
        with patch.object(node_agent, "load_tool", return_value=tool), pytest.raises(ValueError, match="turn"):
            agent.invoke("agent-update", payload)
        agent.runner.assert_not_called()
        agent.download.assert_not_called()
    finally:
        agent.server_close()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux user-service update")
def test_update_repulls_verified_package_and_schedules_supervised_activation(tmp_path):
    agent, tool, payload = agent_at(tmp_path, turns={"present": True, "holder": None})
    try:
        with patch.object(node_agent, "load_tool", return_value=tool):
            result = agent.invoke("agent-update", payload)
        assert result["state"] == "activating"
        assert result["sha256"] == payload["sha256"]
        assert node_update.read(agent.root)["state"] == "prepared"
        agent.download.assert_called_once_with(payload["origin"] + "/node/package.tar.gz", agent.token)
        commands = [call.args[0] for call in agent.runner.call_args_list]
        assert commands[0] == ["systemctl", "--user", "is-active", "hydraweave-node.service"]
        assert commands[1][0] == "systemd-run" and "--on-active=2s" in commands[1]
        assert "hydraweave.node_update" in commands[1]
        assert "--property=Restart=on-failure" in commands[1]
        assert commands[2][1:3] == ["-m", "venv"]
        assert "pip" in commands[3] and "package.tar.gz" in commands[3][-1]
        assert str(agent.root / "agent-venv") not in commands[3][0]
        assert commands[4][-2:] == ["pip", "check"]
        assert not any("sudo" in arg for command in commands for arg in command)
        with pytest.raises(ValueError, match="new work is refused"):
            agent.invoke("lease", {})
    finally:
        agent.server_close()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux user-service update")
def test_bad_package_hash_never_reaches_pip(tmp_path):
    agent, tool, payload = agent_at(tmp_path)
    payload["sha256"] = "0" * 64
    try:
        with patch.object(node_agent, "load_tool", return_value=tool), pytest.raises(ValueError, match="SHA256"):
            agent.invoke("agent-update", payload)
        assert agent.runner.call_count == 2  # service availability + independent supervisor; no install
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
    digest = hashlib.sha256(b"package").hexdigest()
    client.call.side_effect = [{"update_protocol": 1}, {"state": "idle"}, {"state": "activating", "transaction": "a" * 32},
                               {"state": "ready", "id": "a" * 32, "sha256": digest}]
    with patch.object(onboarding, "credentials", return_value={"node1": {"token": "paired"}}), \
            patch.object(onboarding, "Client", return_value=client), \
            patch.object(bootstrap, "source_package", return_value=b"package"):
        result = web.update_node(api, web.update_validate(api, arguments), ctx)
    assert result["state"] == "ready"
    assert [call.args[0] for call in client.call.call_args_list] == ["agent-health", "agent-update-status", "agent-update", "agent-update-status"]
    assert client.call.call_args_list[2].args == ("agent-update", {"origin": arguments["origin"], "sha256": digest})


def test_node_update_job_refuses_unpaired_and_browser_commands(tmp_path):
    machine = SimpleNamespace(id="node1", access="agent")
    api = SimpleNamespace(config_path=tmp_path / "cluster.json", cluster=lambda: SimpleNamespace(machines={"node1": machine}))
    with patch.object(onboarding, "credentials", return_value={}):
        with pytest.raises(ValueError, match="paired"):
            web.update_validate(api, {"machine": "node1", "origin": "http://192.0.2.1"})
    with pytest.raises(ValueError):
        web.update_validate(api, {"machine": "node1", "command": "evil"})


def test_controller_recovers_transaction_when_restart_closes_the_acknowledgement(tmp_path):
    machine = SimpleNamespace(id="node1", access="agent")
    api = SimpleNamespace(config_path=tmp_path / "cluster.json", cluster=lambda: SimpleNamespace(machines={"node1": machine}))
    ctx = Mock()
    ctx.cancelled.return_value = False
    digest = hashlib.sha256(b"package").hexdigest()
    client = Mock()
    client.call.side_effect = [{"update_protocol": 1}, {"id": "previous", "state": "ready"},
                               ConnectionResetError("fixture replacement"),
                               {"id": "new", "state": "ready", "sha256": digest}]
    with patch.object(onboarding, "credentials", return_value={"node1": {"token": "paired"}}), \
            patch.object(onboarding, "Client", return_value=client), \
            patch.object(bootstrap, "source_package", return_value=b"package"):
        result = web.update_node(api, web.update_validate(api, {"machine": "node1", "origin": "http://192.0.2.1:18700"}), ctx)
    assert result["id"] == "new"
    assert [call.args[0] for call in client.call.call_args_list].count("agent-update") == 1


def test_legacy_agent_is_directed_to_staged_bootstrap_without_in_place_update(tmp_path):
    machine = SimpleNamespace(id="node1", access="agent")
    api = SimpleNamespace(config_path=tmp_path / "cluster.json", cluster=lambda: SimpleNamespace(machines={"node1": machine}))
    ctx = Mock()
    ctx.cancelled.return_value = False
    client = Mock()
    client.call.side_effect = PermissionError("unknown node verb")
    with patch.object(onboarding, "credentials", return_value={"node1": {"token": "paired"}}), \
            patch.object(onboarding, "Client", return_value=client), \
            pytest.raises(ValueError, match="bootstrap migration"):
        web.update_node(api, web.update_validate(api, {"machine": "node1", "origin": "http://192.0.2.1:18700"}), ctx)
    client.call.assert_called_once_with("agent-health", {}, timeout=10)


def test_controller_packages_have_stable_digest_across_seconds():
    with patch("gzip.time.time", side_effect=[1000, 2000]):
        assert hashlib.sha256(bootstrap.source_package()).digest() == hashlib.sha256(bootstrap.source_package()).digest()


@pytest.fixture
def update_api(tmp_path):
    machines = {mid: SimpleNamespace(id=mid, access="agent", install_root="/nodes/" + mid)
                for mid in ("node1", "node2")}
    cluster = SimpleNamespace(machines=machines, profiles={"qwen": {}}, router={})
    orchestrator = Mock()
    orchestrator.load_cluster.return_value = cluster
    orchestrator._profile.return_value = (None, None, ["node1"])
    orchestrator._state_path.return_value = tmp_path / "absent-profile.json"
    orchestrator.down.return_value = {"stopped": True}
    api = API(tmp_path / "cluster.json", orchestrator=orchestrator, audit_path=tmp_path / "audit.jsonl")
    api.admission_db = admission.Admission(":memory:")
    onboarding.credentials_path(api.config_path).write_text(json.dumps({
        mid: {"address": "127.0.0." + str(i + 1), "port": 18701, "token": "paired-" + mid}
        for i, mid in enumerate(machines)}))
    reg = features.Registry()
    web.register(reg)
    previous = features.registry()
    features.reset(reg)
    try:
        yield api
    finally:
        api.close_features(timeout_s=3)
        if api._heartbeat is not None:
            api._heartbeat.join(3)
        api.admission_db.close()
        features.reset(previous)


def _finished(api, job, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        row = api.read("jobs/" + job["id"])
        if row["state"] != "running":
            return row
        time.sleep(.01)
    raise AssertionError("job did not finish")


def test_update_admission_allows_other_node_refuses_same_node_and_keeps_stop_available(update_api):
    api = update_api
    entered = {mid: threading.Event() for mid in ("node1", "node2")}
    release = threading.Event()
    digest = hashlib.sha256(b"package").hexdigest()

    def request(address, port, endpoint, body, token, timeout):
        mid = token.removeprefix("paired-")
        verb = body["verb"]
        if verb == "agent-health":
            return {"update_protocol": 1}
        if verb == "agent-update":
            entered[mid].set()
            assert release.wait(5), "fixture update was never released"
            return {"state": "activating", "transaction": mid}
        assert verb == "agent-update-status"
        return {"state": "ready", "id": mid, "sha256": digest} if release.is_set() else {"state": "idle"}

    arguments = {"machine": "node1", "origin": "http://127.0.0.9:18700"}
    with patch.object(onboarding, "request", side_effect=request), \
            patch.object(bootstrap, "source_package", return_value=b"package"):
        try:
            first = api.mutate("discovery/update", arguments)
            assert entered["node1"].wait(2)
            assert first["resources"] == ["node1"] and first["renew"]
            assert api._active is None
            with pytest.raises(APIError, match="held by discovery/update") as refused:
                api.mutate("discovery/update", arguments)
            assert refused.value.status == 409
            second = api.mutate("discovery/update", {**arguments, "machine": "node2"})
            assert entered["node2"].wait(2)
            assert second["resources"] == ["node2"]
            stopped = api.mutate("down", {"profile": "qwen"})
            assert _finished(api, stopped)["result"] == {"stopped": True}
            assert api.read("jobs/" + first["id"])["state"] == "running"
            # The stop has completed while both staging requests are blocked.
            # Observe its shared-admission cancellation before returning the ACK.
            context = api._features_running[first["id"]]
            end = time.monotonic() + 2
            while not context.cancelled() and time.monotonic() < end:
                time.sleep(.01)
            assert context.cancelled()
            assert not api._features_running[second["id"]].cancelled()
            release.set()
            assert _finished(api, first)["state"] == "cancelled"
            assert _finished(api, second)["state"] == "succeeded"
            assert api.admission_db.holds() == []
        finally:
            release.set()
            api.close_features(timeout_s=3)


@pytest.mark.parametrize("changed", ["machine", "pairing"])
def test_update_rejects_identity_changed_after_admission_before_remote_work(update_api, changed):
    api, queued = update_api, []
    with patch.object(api, "_start_worker", side_effect=lambda run, *args: queued.append(run)):
        job = api.mutate("discovery/update", {"machine": "node1", "origin": "http://127.0.0.9:18700"})
    assert api.admission_db.holds()[0]["resources"] == ["node1"]
    if changed == "machine":
        api.cluster().machines["node1"].install_root = "/different/node"
    else:
        saved = onboarding.credentials(api.config_path)
        saved["node1"]["address"] = "127.0.0.8"
        onboarding.credentials_path(api.config_path).write_text(json.dumps(saved))
    with patch.object(onboarding, "request") as request:
        queued.pop()()
    request.assert_not_called()
    result = _finished(api, job)
    assert result["state"] == "failed" and "identity changed" in result["error"]
    assert api.admission_db.holds() == []
    assert "paired-node1" not in json.dumps(job)


def test_transaction_client_pins_private_connection_across_re_adoption(tmp_path):
    path = tmp_path / "cluster.json"
    saved = {"address": "127.0.0.1", "port": 18701, "token": "original"}
    client = onboarding.Client(SimpleNamespace(id="node1"), path, connection=saved)
    saved["address"] = "127.0.0.8"
    onboarding.credentials_path(path).write_text(json.dumps({"node1": dict(saved, token="replacement")}))
    with patch.object(onboarding, "request", return_value={"state": "ready"}) as request:
        client.call("agent-update-status", {}, timeout=5)
    request.assert_called_once_with("127.0.0.1", 18701, "call", {"verb": "agent-update-status", "payload": {}},
                                    "original", 5)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux user-service update")
def test_update_downloads_the_package_from_a_token_protected_portal_with_the_node_credential(tmp_path, monkeypatch):
    """The real downloader against the real gated handler on loopback: the paired node's own credential opens
    the package and nothing else; no credential, a wrong one or an unpaired node stay signed out."""
    import threading
    import urllib.error
    import urllib.request
    from hydraweave import secrets
    from hydraweave.web import server as portal

    portal_token, node_token, data = "a" * 64, "n" * 43, b"node package bytes"
    monkeypatch.delenv("HYDRAWEAVE_NO_AUTH", raising=False)
    monkeypatch.setattr(secrets, "get", lambda name: portal_token)
    monkeypatch.setattr(bootstrap, "package", lambda: (data, hashlib.sha256(data).hexdigest()))
    api = SimpleNamespace(config_path=tmp_path / "cluster.toml", read=Mock(return_value={"ok": True}))
    server = portal.create_server(0, api=api, listen="127.0.0.1")
    server.access_token = portal_token                # token-protected, as a LAN portal is
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    agent, tool, payload = agent_at(tmp_path, turns={"present": True, "holder": None}, data=data)
    agent.download, agent.token = node_agent.download_package, node_token
    payload["origin"] = origin

    def status(path, token=None):
        request = urllib.request.Request(origin + path, headers={"Authorization": "Bearer " + token} if token else {})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    try:
        with patch.object(node_agent, "load_tool", return_value=tool):
            # Not paired with this controller: the node credential opens nothing.
            with pytest.raises(urllib.error.HTTPError, match="401"):
                agent.invoke("agent-update", payload)
            assert not agent.updating
            onboarding.credentials_path(api.config_path).write_text(json.dumps(
                {"node-1": {"address": "127.0.0.1", "port": 1, "token": node_token}}))
            assert agent.invoke("agent-update", payload)["state"] == "activating"
        assert status("/node/package.tar.gz") == 401
        assert status("/node/package.tar.gz", "wrong") == 401
        assert status("/node/package.tar.gz", portal_token) == 200
        # The node credential is not a portal credential.
        for path in ("/node/bootstrap.sh", "/hydraweave/api/devices", "/metrics", "/app.js"):
            assert status(path, node_token) == 401, path
        api.read.assert_not_called()
    finally:
        server.shutdown()
        server.server_close()
        agent.server_close()
