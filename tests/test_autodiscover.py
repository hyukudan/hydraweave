"""No root or external network: DNS-SD fakes, multicast on loopback and two HTTP fake nodes."""
import copy
import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tarfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

from unittest import SkipTest

try:
    import pytest
except ImportError:
    raise SkipTest("requires the test extra (pytest)") from None

from hydraweave import bootstrap, config, discovery, node_agent, onboarding, provisioning

ROOT = Path(__file__).resolve().parents[1]


def record(name="one", address="127.0.0.1", port=18701):
    return {"id": name, "hostname": name, "agent_version": "0.1.0", "gpu_arch": "gfx1151",
            "unified_memory": 128 << 30, "engine_version": "", "usb4_ids": [name],
            "usb4_peers": ["two" if name == "one" else "one"], "address": address, "port": port}


def raw_config():
    return {"schema": "hydraweave/cluster-v1", "machines": [
        {"id": "controller", "os": "linux", "access": "local", "http_address": "127.0.0.8",
         "install_root": "/home/controller/hydraweave", "lab_mode": False}], "assets": {}, "profiles": {}}


def test_dns_packet_ttl_goodbye_malformed_and_no_secrets():
    now = [0]
    registry = discovery.Registry(clock=lambda: now[0])
    row = record()
    data = discovery.packet(row)
    assert b"pairing" not in data and b"token" not in data
    discovery.receive(data, "127.0.0.1", registry)
    assert registry.rows()[0]["gpu_arch"] == "gfx1151"
    assert registry.rows()[0]["state"] == "discovered, not adopted"
    for invalid in (b"", data[:20], b"\0" * 9000, b"\xff" * 100):
        assert discovery.receive(invalid, "127.0.0.1", registry) is False
    discovery.receive(discovery.packet(row, 0), "127.0.0.1", registry)
    assert registry.rows() == []
    registry.update(row)
    now[0] = 91
    assert registry.rows() == []
    query = struct.pack("!6H", 0, 0, 1, 0, 0, 0) + discovery._name(discovery.SERVICE) + struct.pack("!HH", 12, 1)
    assert discovery.receive(query, "127.0.0.1", registry)


def test_fake_zeroconf_announce_browse_remove_close():
    registry = discovery.Registry()
    info = SimpleNamespace(properties=discovery.properties(record()), port=18701,
                           parsed_addresses=lambda: ["127.0.0.1"])
    instance = Mock()
    instance.get_service_info.return_value = info
    backend = SimpleNamespace(Zeroconf=Mock(return_value=instance), IPVersion=SimpleNamespace(V4Only=4),
                              ServiceInfo=Mock(return_value=info), ServiceBrowser=Mock())
    service = discovery.DNSService(registry, record(), backend=backend)
    listener = backend.ServiceBrowser.call_args.args[2]
    listener.add_service(instance, discovery.SERVICE, "one." + discovery.SERVICE)
    assert registry.rows()[0]["id"] == "one"
    instance.register_service.assert_called_once_with(info)
    service.refresh(dict(record(), engine_version="new-release"))
    instance.update_service.assert_called_once_with(info)
    listener.remove_service(instance, discovery.SERVICE, "one." + discovery.SERVICE)
    assert registry.rows() == []
    service.close()
    instance.unregister_service.assert_called_once_with(info)
    instance.close.assert_called_once()


@pytest.mark.parametrize("field,value", [("id", "bad.name"), ("hostname", {}),
                         ("gpu_arch", ["gfx1151"]), ("unified_memory", True),
                         ("usb4_ids", [["nested"]]), ("usb4_peers", "not-a-list")])
def test_malformed_lan_metadata_is_ignored_without_breaking_browsing(field, value):
    registry = discovery.Registry()
    discovery.receive(discovery.packet(dict(record(), **{field: value})), "127.0.0.1", registry)
    assert registry.rows() == []
    discovery.receive(discovery.packet(record()), "127.0.0.1", registry)
    assert len(registry.rows()) == 1


def test_txt_allowlist_never_advertises_credentials_or_local_paths():
    values = discovery.properties(dict(record(), token="secret", code="12345678", install_root="/private"))
    assert not {"token", "code", "install_root"} & values.keys()
    # Foreign stacks may add fields; ignore them without decoding their untrusted contents.
    values[b"unknown"] = b"not-json"
    assert "unknown" not in discovery.from_properties(values, "127.0.0.1", 18701)


def test_multicast_loopback_browse_and_announce():
    # Real UDP sockets, but the membership and outbound interface are loopback only.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    registry = discovery.Registry()
    browser = discovery.Multicast(registry, interface="127.0.0.1", port=port, group="239.255.71.71")
    agent = discovery.Multicast(discovery.Registry(), record(), interface="127.0.0.1", port=port, group="239.255.71.71")
    try:
        deadline = time.monotonic() + 3
        while not registry.rows() and time.monotonic() < deadline:
            time.sleep(.02)
        if not registry.rows() and os.name == "nt":
            pytest.skip("Windows does not route IPv4 multicast on the loopback interface; fake bus is tested separately")
        assert registry.rows()[0]["id"] == "one"
    finally:
        agent.close()
        browser.close()


def test_missing_zeroconf_falls_back():
    with patch.object(discovery.importlib, "import_module", side_effect=ImportError), \
            patch.object(discovery, "Multicast") as fallback:
        service = discovery.DNSService(discovery.Registry(), record())
        service.close()
        fallback.return_value.close.assert_called_once()


def test_multicast_transport_with_loopback_socket_bus():
    # Model multicast membership on platforms without loopback multicast routing, using real loopback UDP.
    factory, members = socket.socket, []

    class BusSocket:
        def __init__(self, *_):
            self.actual = factory(socket.AF_INET, socket.SOCK_DGRAM)

        def setsockopt(self, *_):
            pass

        def bind(self, address):
            self.actual.bind(("127.0.0.1", 0))
            members.append(self)

        def settimeout(self, seconds):
            self.actual.settimeout(seconds)

        def sendto(self, data, destination):
            for member in list(members):
                self.actual.sendto(data, member.actual.getsockname())

        def recvfrom(self, size):
            return self.actual.recvfrom(size)

        def close(self):
            members.remove(self)
            self.actual.close()

    registry = discovery.Registry()
    with patch.object(discovery.socket, "socket", BusSocket):
        browser = discovery.Multicast(registry)
        advertiser = discovery.Multicast(discovery.Registry(), record())
        try:
            deadline = time.monotonic() + 2
            while not registry.rows() and time.monotonic() < deadline:
                time.sleep(.01)
            assert registry.rows()[0]["id"] == "one"
        finally:
            advertiser.close()
            browser.close()


def provision_cluster():
    raw = raw_config()
    raw["machines"].append({"id": "node-a", "os": "linux", "access": "agent", "http_address": "192.0.2.1",
                            "install_root": "/home/operator/hydraweave", "lab_mode": True,
                            "lab_dir": "/home/operator/lab", "devices": [{"id": "gpu", "type": "amd"}]})
    raw["profiles"] = {"qwen": {"preset": "qwen-replicas", "nodes": ["node-a"]}}
    return config.parse(raw)


def model_source():
    return {"status": "download", "repo": "test/model", "revision": "b" * 40,
            "files": [{"file": "model.gguf", "bytes": 10, "sha256": "c" * 64}]}


def test_provision_plan_exact_recipe_models_and_lab_root_reuse():
    cluster = provision_cluster()
    with patch.object(provisioning.downloads, "sources", return_value={"model": model_source()}):
        result = provisioning.plan(cluster, "node-a", "qwen")
        assert result["engine"]["components"]["engine"]["commit"] == config.preset("qwen-replicas")["engine_sha"]
        assert result["engine"]["lab_dir"] == "/home/operator/lab"
        assert result["models"][0]["manifest"]["sha256"] == "c" * 64
        assert result["model_roots"] == ["/home/operator/lab/models"]
        assert not result["starts_inference"]
        inventory = {"roots": [{"path": result["model_roots"][0], "units": [
            {"name": "model.gguf", "bytes": 10, "kind": "file"}]}]}
        reused = provisioning.plan(cluster, "node-a", "qwen", inventory)
        assert reused["models"][0]["existing"]["path"] == "/home/operator/lab/models/model.gguf"
    with patch.object(provisioning.downloads, "sources", return_value={"model": {"status": "unavailable", "why": "private model"}}):
        assert "private model" in provisioning.plan(cluster, "node-a", "qwen")["blockers"][0]
    with patch.object(provisioning.engines, "components", return_value={"engine": {"commit_prefix": "1234567"}}):
        assert any("bundle" in text for text in provisioning.plan(cluster, "node-a", "qwen")["blockers"])


def test_existing_download_plan_resume_disk_check_and_checksum(tmp_path):
    module = node_agent.load_tool().extension_for("download")
    directory = tmp_path / "models"
    directory.mkdir()
    (directory / module.ASSET_MARKER).touch()
    part = directory / ".model.gguf.hydraweave-part"
    part.write_bytes(b"1234")
    source = model_source()
    item = dict(source["files"][0], repo=source["repo"], revision=source["revision"],
                directory=str(directory), destination=str(directory / "model.gguf"))
    payload = {"root": str(tmp_path / "install"), "files": [item]}
    with patch.object(module.shutil, "which", return_value="/usr/bin/curl"), \
            patch.object(module.shutil, "disk_usage", return_value=SimpleNamespace(free=module.HEADROOM + 5)):
        result = module.plan(payload, tmp_path / "install")
        assert result["disk"][0]["required_bytes"] == 6
        assert not result["disk"][0]["ok"]
        assert "--continue-at" in result["files"][0]["argv"]
        assert result["files"][0]["sha256"] == "c" * 64
        with pytest.raises(ValueError, match="space"):
            module.start(tmp_path / "install", dict(payload, confirm=result["confirm"]))
        changed = copy.deepcopy(payload)
        changed["files"][0]["sha256"] = "d" * 64
        assert module.plan(changed, tmp_path / "install")["confirm"] != result["confirm"]


def test_no_announce_flag_does_not_construct_transport(tmp_path):
    server = Mock(token=None, code="12345678", record=record())
    server.serve_forever.side_effect = KeyboardInterrupt
    with patch.object(node_agent, "Agent", return_value=server), patch.object(discovery, "DNSService") as dns:
        assert node_agent.main(["serve", "--root", str(tmp_path / "agent"), "--no-announce"]) == 0
    dns.assert_not_called()
    server.server_close.assert_called_once()


def test_lan_address_uses_route_when_ubuntu_hostname_is_loopback():
    route = Mock()
    route.__enter__ = Mock(return_value=route)
    route.__exit__ = Mock(return_value=False)
    route.getsockname.return_value = ("192.0.2.10", 12345)
    with patch.object(node_agent.socket, "socket", return_value=route):
        assert node_agent.Agent.lan_address() == "192.0.2.10"
    route.connect.assert_called_once_with((discovery.GROUP, discovery.PORT))
    route.sendto.assert_not_called()


def test_usb4_pair_requires_mutual_host_identity():
    assert discovery.pairs([record(), record("two")]) == [["one", "two"]]
    other = record("two")
    other["usb4_ids"] = ["unrelated"]
    assert discovery.pairs([record(), other]) == []


def test_usb4_identity_reuses_existing_reader():
    tool = node_agent.load_tool()
    module = tool.extension_for("usb4")
    reader = Mock()
    reader.listdir.return_value = ["0-0", "0-1", "domain0"]
    reader.read.return_value = "own-uuid"
    with patch.object(module.Collector, "domains", return_value={}), \
            patch.object(module.Collector, "links", return_value=[{"peer_unique_id": "peer-uuid"}]):
        assert module.identity(reader) == {"usb4_ids": ["own-uuid"], "usb4_peers": ["peer-uuid"]}
    reader.read.assert_called_once_with("/sys/bus/thunderbolt/devices/0-0/unique_id")


def test_prepare_preserves_real_layout_and_user_unit(tmp_path):
    root = tmp_path / "hydraweave"
    (root / "etc").mkdir(parents=True)
    (root / "log").mkdir()
    (root / "etc/settings.json").write_text("keep settings")
    (root / "log/node.log").write_text("keep logs")
    node_agent.prepare(root)
    first = (root / "etc/agent-id").read_text()
    calls = []
    with patch.object(Path, "home", return_value=tmp_path):
        node_agent.install_service(root, str(tmp_path / "lab"), run=lambda args, **kwargs: calls.append(args))
    assert (root / "etc/settings.json").read_text() == "keep settings"
    assert (root / "log/node.log").read_text() == "keep logs"
    assert (root / "etc/agent-id").read_text() == first
    assert all(call[:2] == ["systemctl", "--user"] for call in calls[:2])
    assert [call[2] for call in calls[:2]] == ["daemon-reload", "enable"]
    assert calls[1][3] == "--now"
    unit = (tmp_path / ".config/systemd/user/hydraweave-node.service").read_text()
    assert "--lab-dir" in unit and "sudo" not in unit
    assert "KillMode=process" in unit
    with patch.object(Path, "home", return_value=tmp_path), patch.object(node_agent.subprocess, "run"), \
            patch.object(node_agent.sys, "platform", "linux"):
        node_agent.main(["install-service", "--root", str(root), "--no-announce"])
    unit = (tmp_path / ".config/systemd/user/hydraweave-node.service").read_text()
    assert "--no-announce" in unit
    assert node_agent.main([]) == 0
    (root / "etc/agent-id").write_text("unexpected identity")
    with pytest.raises(ValueError, match="identity"):
        node_agent.prepare(root)


def test_bootstrap_render_never_executes_sudo_and_shell_syntax(tmp_path):
    script = bootstrap.script("http://127.0.0.1:18700").decode()
    before, after = script.split("missing=''\n", 1)
    assert "sudo apt-get" in after
    assert "amdgpu-dkms" not in after
    assert "hydraweave-node pair" in after
    assert "sudo" not in before
    assert "trap 'rm -rf \"$package_dir\"' EXIT" in script
    assert 'rm -rf "$root"' not in script and "systemctl --system" not in script
    assert "agent-venv" in script and "--upgrade" in script
    assert "--force-reinstall" in script  # Checkout builds can retain the same project version.
    path = tmp_path / "bootstrap.sh"
    path.write_bytes(script.encode())
    bash = shutil.which("bash")
    if bash:
        subprocess.run([bash, "-n", str(path)], check=True, capture_output=True)
    shellcheck = shutil.which("shellcheck")
    if shellcheck:
        subprocess.run([shellcheck, "-s", "sh", str(path)], check=True, capture_output=True)
    with pytest.raises(ValueError):
        bootstrap.script("http://host/'$(oops)")


def test_bootstrap_archive_ships_all_node_extensions_and_packaging_metadata():
    data = bootstrap.source_package()
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        names = set(archive.getnames())
    assert {"hydraweave/node/hydraweave-node", "hydraweave/node/hydraweave_install.py",
            "hydraweave/node/hydraweave_usb4.py", "hydraweave/node/build-node.sh",
            "hydraweave/versions.lock", "hydraweave/pyproject.toml"} <= names
    assert not any("__pycache__" in name or "cluster.json" in name for name in names)


def test_pairing_refusal_expiry_replay_and_root_boundary(tmp_path):
    agent = node_agent.Agent(("127.0.0.1", 0), tmp_path / "node", record=record())
    try:
        with pytest.raises(PermissionError):
            agent.pair("wrong")
        answer = agent.pair(agent.code)
        assert len(answer["token"]) > 30
        with pytest.raises(PermissionError):
            agent.pair(agent.code)
        with pytest.raises(ValueError, match="root"):
            agent.invoke("doctor", {"root": "/another-user"})
        with pytest.raises(ValueError, match="root"):
            agent.invoke("poll", {"parts": [{"payload": {"root": "/another-user"}}]})
    finally:
        agent.server_close()
    expired = node_agent.Agent(("127.0.0.1", 0), tmp_path / "expired", record=record())
    try:
        old = expired.code
        fresh = expired.reissue_code()
        assert fresh["code"] != old and fresh["expires_in"] == 600
        expired.code_expires = 0
        with pytest.raises(PermissionError):
            expired.pair(expired.code)
    finally:
        expired.server_close()


def test_service_portal_unit_and_injected_runner(tmp_path):
    calls = []
    with patch.object(Path, "home", return_value=tmp_path), patch.object(node_agent.sys, "platform", "linux"):
        node_agent.service_command("install", run=lambda args, **kwargs: calls.append(args))
    unit = (tmp_path / ".config/systemd/user/hydraweave.service").read_text()
    assert "-m hydraweave ui --no-browser" in unit
    assert "Restart=on-failure" in unit
    assert calls[:2] == [["systemctl", "--user", "daemon-reload"],
                         ["systemctl", "--user", "enable", "--now", "hydraweave.service"]]


def test_service_start_stop_remove_and_status_exit_code(tmp_path):
    calls = []

    def runner(args, **kwargs):
        calls.append((args, kwargs.get("check")))
        return subprocess.CompletedProcess(args, 3)

    with patch.object(Path, "home", return_value=tmp_path), patch.object(node_agent.sys, "platform", "linux"):
        node_agent.service_command("start", run=runner)
        node_agent.service_command("stop", run=runner)
        assert node_agent.service_command("status", run=runner) == 3
        node_agent.service_command("remove", run=runner)
    assert [args for args, _ in calls] == [
        ["systemctl", "--user", "start", "hydraweave.service"],
        ["systemctl", "--user", "stop", "hydraweave.service"],
        ["systemctl", "--user", "status", "hydraweave.service"],
        ["systemctl", "--user", "disable", "--now", "hydraweave.service"],
        ["systemctl", "--user", "daemon-reload"]]
    assert calls[2][1] is False
    with patch.object(node_agent.sys, "platform", "win32"):
        assert node_agent.service_command("start", run=runner) == 2


def test_bootstrap_retries_pairing_and_keeps_messages_on_one_line():
    script = bootstrap.script("http://127.0.0.1:18700").decode()
    assert "until \"$root/agent-venv/bin/hydraweave-node\" pair" in script
    assert "Reissue pairing code: hydraweave-node pair" in script
    for line in script.splitlines():
        if 'missing="$missing' in line:
            assert "$missing\\n" in line


def test_matching_keeps_stable_ids_and_refuses_ambiguity():
    raw = raw_config()
    machine = {"id": "node-a", "http_address": "10.0.0.1", "ssh_target": "operator@one.local"}
    raw["machines"].append(machine)
    assert onboarding.match(raw, record()) is machine
    assert onboarding.match(raw, dict(record(), hostname="different", address="10.0.0.1")) is machine
    saved = {"node-a": {"usb4_peers": ["two"]}}
    assert onboarding.match(raw, dict(record(), hostname="different"), saved) is machine
    raw["machines"].append(dict(machine, id="node-b"))
    with pytest.raises(ValueError, match="ambiguous"):
        onboarding.match(raw, record())


def test_two_fake_nodes_end_to_end_discover_pair_adopt_provision(tmp_path):
    raw = raw_config()
    config_path = tmp_path / "cluster.json"
    raw["machines"].append({"id": "node-a", "os": "linux", "access": "ssh", "http_address": "127.0.0.2",
                            "ssh_target": "operator@one", "install_root": str(tmp_path / "first"),
                            "lab_mode": True, "lab_dir": "/home/operator/lab",
                            "devices": [{"id": "gpu", "type": "amd"}], "peer_ssh": {}})
    # On Windows strict config uses Linux remote POSIX paths; the agents in this integration test use temp paths.
    raw["machines"][1]["os"] = "windows" if os.name == "nt" else "linux"
    config_path.write_text(json.dumps(raw))
    registry = discovery.Registry()
    controller = onboarding.Controller(config_path, registry=registry)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    browser = discovery.Multicast(registry, interface="127.0.0.1", port=port, group="239.255.71.72")
    agents, threads = [], []
    calls = []

    def call(verb, payload):
        calls.append((verb, payload))
        return {"schema": "hydraweave/node-v1", "verb": verb, "job": "fake-job", "state": "succeeded",
                "release_id": "a" * 12, "releases": [], "assets": {}}

    class FakeAgent(node_agent.Agent):
        def invoke(self, verb, payload):
            if verb == "provision-prepare":
                self.check_paths(payload)
                return {"prepared": True}
            return super().invoke(verb, payload)

    try:
        for name, address, directory in [("one", "127.0.0.2", "first"), ("two", "127.0.0.3", "second")]:
            agent = FakeAgent((address, 0), tmp_path / directory,
                                     lab_dir="/home/operator/lab" if name == "one" else None,
                                     record=record(name, address), call=call)
            agents.append(agent)
            thread = threading.Thread(target=agent.serve_forever, daemon=True)
            thread.start()
            threads.append(thread)
            # Real DNS-SD datagrams over loopback. Unicast delivery also works on Windows's multicast proxy.
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                sender.bind((address, 0))
                sender.sendto(discovery.packet({k: v for k, v in agent.record.items()
                                                 if k not in {"install_root", "lab_dir", "user"}}), ("127.0.0.1", port))
        deadline = time.monotonic() + 2
        while len(registry.rows()) < 2 and time.monotonic() < deadline:
            time.sleep(.01)
        assert controller.inventory()["pairs"] == [["one", "two"]]
        with pytest.raises(PermissionError):
            controller.adopt("one", "000000000")
        first = controller.adopt("one", agents[0].code)
        assert first == {"adopted": "node-a", "merged": True}
        # Validate the new node's POSIX remote root separately on Windows (its HTTP daemon remains loopback).
        if os.name == "nt":
            agents[1].record["install_root"] = "/home/operator/hydraweave"
        second = controller.adopt("two", agents[1].code)
        assert second["adopted"] == "two"
        updated = config.read_json(config_path)
        assert updated["machines"][1] == raw["machines"][1]
        assert len(updated["machines"]) == 3
        assert controller.adopt("one", "unused")["already_adopted"] is True
        assert "token" not in json.dumps(controller.inventory())
        assert not config_path.with_name("node-agents.json.prev").exists()
        machine = SimpleNamespace(id="node-a", install_root=str(agents[0].root))
        reply = onboarding.Client(machine, config_path).call("doctor", {"root": str(agents[0].root)})
        assert reply["verb"] == "doctor"
        desired = {"engine": {"root": str(agents[0].root), "lab_dir": agents[0].lab_dir,
                               "components": {}, "recipe": {}}, "models": [], "manifests": {},
                   "model_roots": [], "blockers": []}
        cluster = SimpleNamespace(machines={"node-a": machine}, profiles={"profile": {"preset": "qwen-replicas"}})
        with patch.object(provisioning, "plan", return_value=desired), \
                patch.object(provisioning.orchestrate, "_stage_bundles", return_value={}), \
                patch.object(provisioning.orchestrate, "_components", return_value={}):
            result = provisioning.execute(cluster, config_path, "node-a", "profile")
        assert result["release_id"] == "a" * 12
        assert {verb for verb, _ in calls} >= {"install", "job", "activate"}
        assert not {verb for verb, _ in calls} & {"lease", "peer-run", "service-start"}
    finally:
        browser.close()
        for agent in agents:
            agent.shutdown()
            agent.server_close()
        for thread in threads:
            thread.join(timeout=2)


def test_package_recipes_dry_run(tmp_path):
    spec = importlib.util.spec_from_file_location("node_packaging", ROOT / "packaging/build.py")
    package = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(package)
    with patch.object(package.subprocess, "run") as run:
        for kind in ("deb", "rpm"):
            result = package.build(kind, tmp_path / kind, dry_run=True)
            assert result["command"][:5] == ["fpm", "-s", "dir", "-t", kind]
            stage = Path(result["stage"])
            assert (stage / "usr/lib/hydraweave/node/hydraweave_install.py").is_file()
            unit = (stage / "usr/lib/systemd/user/hydraweave-node.service").read_text()
            assert "sudo" not in unit and "%h/hydraweave" in unit
            (stage / "usr/lib/hydraweave/obsolete.py").write_text("from an older build")
            rebuilt = Path(package.build(kind, tmp_path / kind, dry_run=True)["stage"])
            assert rebuilt != stage
            assert not (rebuilt / "usr/lib/hydraweave/obsolete.py").exists()
    run.assert_not_called()


def test_provision_progress_is_bounded_and_deduplicated():
    client = Mock()
    running = {"state": "running", "progress": {"file": "model.gguf", "bytes": 5, "total_bytes": 10}}
    client.call.side_effect = [running, running, {"state": "succeeded"}]
    report = Mock()
    with patch.object(provisioning.time, "sleep"):
        assert provisioning._wait(client, "/root", "job", report, lambda: False)["state"] == "succeeded"
    assert report.call_args_list[0].args == ("model.gguf: 5/10 bytes",)
    assert report.call_count == 2


def test_verified_source_bundle_chunks_and_offset_guard(tmp_path):
    agent = node_agent.Agent(("127.0.0.1", 0), tmp_path / "node", record=record())
    stage = agent.root / "incoming" / (".stage-" + "a" * 32 + ".bundle")
    body = {"root": str(agent.root), "stage": str(stage), "bytes": 6, "offset": 0,
            "data": base64.b64encode(b"abc").decode()}
    try:
        with patch.object(node_agent.shutil, "disk_usage", return_value=SimpleNamespace(free=100 << 30)):
            assert agent.invoke("bundle-chunk", body)["offset"] == 3
            with pytest.raises(ValueError, match="offset"):
                agent.invoke("bundle-chunk", body)
            assert agent.invoke("bundle-chunk", dict(body, offset=3))["offset"] == 6
            assert stage.read_bytes() == b"abcabc"
            with pytest.raises(ValueError, match="staging"):
                agent.invoke("bundle-chunk", dict(body, stage=str(tmp_path / "escape")))
        with patch.object(node_agent.shutil, "disk_usage", return_value=SimpleNamespace(free=0)):
            with pytest.raises(ValueError, match="space"):
                agent.invoke("bundle-chunk", dict(body, offset=6, data=""))
    finally:
        agent.server_close()


def test_default_agent_port_collision_uses_advertised_dynamic_port(tmp_path):
    import errno
    server = Mock(token=None, code="12345678", record=record(), server_port=34567)
    server.serve_forever.side_effect = KeyboardInterrupt
    with patch.object(node_agent, "Agent", side_effect=[OSError(errno.EADDRINUSE, "busy"), server]) as factory:
        node_agent.main(["serve", "--root", str(tmp_path / "node"), "--no-announce"])
    assert factory.call_args_list[1].args[0] == ("0.0.0.0", 0)


def test_agent_refuses_rebound_host_names(tmp_path):
    agent = node_agent.Agent(("127.0.0.1", 0), tmp_path / "node", record=record())
    thread = threading.Thread(target=agent.serve_forever, daemon=True)
    thread.start()
    try:
        import http.client
        for host, status in (("attacker.example:" + str(agent.server_port), 403),
                             ("127.0.0.1:" + str(agent.server_port), 403)):
            connection = http.client.HTTPConnection("127.0.0.1", agent.server_port, timeout=5)
            connection.putrequest("POST", "/pair", skip_host=True)
            body = json.dumps({"code": "00000000"}).encode()
            connection.putheader("Host", host)
            connection.putheader("Content-Length", str(len(body)))
            connection.endheaders(body)
            reply = connection.getresponse()
            error = json.loads(reply.read())["error"]
            assert reply.status == status
            # A rebound name is refused before the pairing code is even compared (no failed attempt counted).
            assert ("Host" in error) == host.startswith("attacker")
            connection.close()
        assert len(agent.failures) == 1
    finally:
        agent.shutdown()
        agent.server_close()
        thread.join(timeout=2)


def test_empty_hostnames_never_match_and_lab_mode_is_explicit(tmp_path, monkeypatch):
    raw = raw_config()
    raw["machines"].append({"id": "node-a", "install_root": "/srv/hydraweave"})   # no address, no SSH target
    assert onboarding.match(raw, dict(record(), hostname="", address="10.0.0.9")) is None
    monkeypatch.delenv("HYDRAWEAVE_LAB_DIR", raising=False)
    assert node_agent.default_lab_dir() is None
    monkeypatch.setenv("HYDRAWEAVE_LAB_DIR", str(tmp_path))
    assert node_agent.default_lab_dir() == str(tmp_path)
    monkeypatch.setenv("HYDRAWEAVE_LAB_DIR", str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="HYDRAWEAVE_LAB_DIR"):
        node_agent.default_lab_dir()


def test_controller_serves_bootstrap_and_source_over_loopback(tmp_path):
    from hydraweave.web.server import PortalServer
    server = PortalServer(0, api=SimpleNamespace(config_path=tmp_path / "cluster.json"), listen="127.0.0.1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        origin = "http://127.0.0.1:" + str(server.server_port)
        opener = build_opener(ProxyHandler({}))
        with opener.open(origin + "/node/bootstrap.sh") as response:
            script = response.read()
            assert origin.encode() + b"/node/package.tar.gz" in script
            assert response.headers["Content-Type"] == "text/x-shellscript"
        with opener.open(origin + "/node/package.tar.gz") as response:
            assert response.read(2) == b"\x1f\x8b"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_node_preparation_checks_disk_and_preserves_lab_tree(tmp_path):
    agent = node_agent.Agent(("127.0.0.1", 0), tmp_path / "node", record=record())
    install = node_agent.load_tool().extension_for("install")
    directory = tmp_path / "hydraweave-models" / "model"
    try:
        with patch.object(install, "checked_root", return_value=agent.root), \
                patch.object(node_agent.shutil, "disk_usage", return_value=SimpleNamespace(free=100 << 30)):
            result = agent.invoke("provision-prepare", {"root": str(agent.root), "directories": [str(directory)]})
            assert result["disk"]["required_bytes"] == 8 << 30
            assert (directory / ".hydraweave-assets").is_file()
            with pytest.raises(ValueError, match="dedicated"):
                agent.invoke("provision-prepare", {"directories": [str(tmp_path / "lab/models")]})
        assert not (tmp_path / "lab").exists()
        with patch.object(install, "checked_root", return_value=agent.root), \
                patch.object(node_agent.shutil, "disk_usage", return_value=SimpleNamespace(free=0)):
            with pytest.raises(ValueError, match="8 GiB"):
                agent.invoke("provision-prepare", {"directories": []})
    finally:
        agent.server_close()


def test_download_progress_failure_reaps_child_and_keeps_resume_file(tmp_path):
    module = node_agent.load_tool().extension_for("download")
    part = tmp_path / "model.part"
    part.write_bytes(b"partial")
    child = Mock(pid=4242)
    child.poll.return_value = None
    row = {"part": str(part), "destination": str(tmp_path / "model.gguf"), "bytes": 10, "argv": ["fake-curl"]}
    with patch.object(module, "lock_part", return_value=None), \
            patch.object(module.subprocess, "Popen", return_value=child), \
            patch.object(module.os, "killpg", create=True) as terminate:
        with pytest.raises(OSError, match="progress write"):
            module.fetch(row, lambda: None, lambda: None, Mock(side_effect=OSError("progress write failed")))
    terminate.assert_called_once_with(4242, module.signal.SIGTERM)
    child.wait.assert_called_once()
    assert part.read_bytes() == b"partial"


def test_same_host_announcement_uses_its_a_record_but_lan_sources_win():
    registry = discovery.Registry()
    discovery.receive(discovery.packet(record("one", "10.0.0.5")), "127.0.0.1", registry)
    discovery.receive(discovery.packet(record("two", "10.0.0.99")), "10.0.0.6", registry)
    rows = {row["id"]: row["address"] for row in registry.rows()}
    assert rows == {"one": "10.0.0.5", "two": "10.0.0.6"}
