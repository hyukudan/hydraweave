"""Release transactions: real HTTP instances, retained environments and concurrent control."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch
import venv

try:
    import pytest
except ImportError:  # unittest discovery before installation of the optional test extra
    from unittest import SkipTest
    raise SkipTest("requires the test extra (pytest)") from None

from hydraweave import __version__, discovery, node_agent, node_update
from tests.test_node_update import agent_at

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux user-service transactions")
ROOT = Path(__file__).resolve().parents[1]


def test_default_agent_routes_staging_through_owned_command_runner(tmp_path):
    def staged(root, row, data, *, runner):
        assert runner is None, "production staging bypassed its owned-child runner"
        return node_update.write(root, row, state="staged")

    # Any accidental default subprocess.run invocation fails before touching a
    # real service. The independent runner itself has real process tests below.
    with patch.object(subprocess, "run", side_effect=AssertionError("unowned command")), \
            patch.object(node_update, "_run") as command, \
            patch.object(node_update, "schedule") as schedule, \
            patch.object(node_update, "stage", side_effect=staged), \
            patch.object(node_agent.Agent, "_update_idle"):
        agent = node_agent.Agent(("127.0.0.1", 0), tmp_path / "node",
                                 record={"address": "127.0.0.1"}, download=lambda *args: b"package")
        try:
            result = agent.invoke("agent-update", {"origin": "http://192.0.2.1:18700",
                                                   "sha256": hashlib.sha256(b"package").hexdigest()})
            assert result["state"] == "activating"
            assert schedule.call_args.kwargs["runner"] is None
            assert command.call_args.args[1] is None
            assert command.call_args.kwargs["timeout"] == 30
        finally:
            agent.server_close()


def test_update_command_keeps_bounded_diagnostics():
    result = node_update._run([sys.executable, "-c", "import sys; sys.stdout.write('x' * 700000 + 'done')"])
    assert result.returncode == 0
    assert result.stdout.endswith("done")
    assert len(result.stdout) <= 256 * 1024
    with pytest.raises(ValueError, match="build failed"):
        node_update._run([sys.executable, "-c", "import sys; sys.stderr.write('build failed'); sys.exit(2)"])


def test_update_timeout_kills_term_resistant_build_descendants(tmp_path):
    marker = tmp_path / "build-processes.json"
    code = (
        "import json,os,signal,subprocess,sys,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "child=subprocess.Popen([sys.executable,'-c',"
        "'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)']); "
        f"Path({str(marker)!r}).write_text(json.dumps([os.getpid(),child.pid])); time.sleep(60)"
    )
    descriptors = []
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            node_update._run([sys.executable, "-c", code], timeout=2)
        assert marker.exists(), "fixture did not start its build processes"
        for pid in json.loads(marker.read_text()):
            try:
                descriptor = os.pidfd_open(pid)
            except ProcessLookupError:
                continue
            descriptors.append(descriptor)
            assert select.select([descriptor], [], [], 3)[0], "build descendant survived update timeout"
    finally:
        for descriptor in descriptors:
            try:
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.close(descriptor)


def test_status_and_stop_complete_while_package_download_is_blocked(tmp_path):
    agent, tool, payload = agent_at(tmp_path)
    entered, release = threading.Event(), threading.Event()
    results = []
    def download(*args):
        entered.set()
        assert release.wait(5)
        return b"package"
    agent.download = download
    agent.call.return_value = {"ok": True}
    def update():
        try:
            results.append(agent.invoke("agent-update", payload))
        except Exception as error:
            results.append(error)
    try:
        with patch.object(node_agent, "load_tool", return_value=tool):
            worker = threading.Thread(target=update)
            worker.start()
            assert entered.wait(5)
            # A deadlock fails with a bounded fixture timeout rather than holding the suite.
            completed = threading.Event()
            def controls():
                assert agent.invoke("status", {}) == {"ok": True}
                assert agent.invoke("stop", {}) == {"ok": True}
                assert agent.invoke("agent-update-status", {})["state"] == "staging"
                completed.set()
            reader = threading.Thread(target=controls)
            reader.start()
            assert completed.wait(1), "control calls waited behind package download"
            with pytest.raises(ValueError, match="new work is refused"):
                agent.invoke("lease", {})
            release.set()
            worker.join(5)
            reader.join(5)
        assert results[0]["state"] == "activating", results
    finally:
        release.set()
        agent.server_close()


def test_update_refuses_an_already_running_mutation(tmp_path):
    agent, tool, payload = agent_at(tmp_path)
    entered, release = threading.Event(), threading.Event()
    def mutation(*args):
        entered.set()
        assert release.wait(5)
        return {}
    agent.call = mutation
    worker = threading.Thread(target=lambda: agent.invoke("install", {}))
    try:
        worker.start()
        assert entered.wait(5)
        with patch.object(node_agent, "load_tool", return_value=tool), pytest.raises(ValueError, match="still active"):
            agent.invoke("agent-update", payload)
        agent.download.assert_not_called()
    finally:
        release.set()
        worker.join(5)
        agent.server_close()


def test_import_outside_candidate_fails_without_changing_active_environment(tmp_path):
    agent, tool, payload = agent_at(tmp_path)
    active = agent.root / "agent-venv/active-marker"
    active.write_text("old environment")
    original = agent.runner.side_effect
    def commands(argv, **kwargs):
        result = original(argv, **kwargs)
        if "-c" in argv:
            data = json.loads(result.stdout)
            data["module"] = str(active.parent / "node_agent.py")
            result.stdout = json.dumps(data)
        return result
    agent.runner.side_effect = commands
    try:
        with patch.object(node_agent, "load_tool", return_value=tool), pytest.raises(ValueError, match="outside"):
            agent.invoke("agent-update", payload)
        assert active.read_text() == "old environment"
        assert not list((agent.root / "agent-releases").iterdir())
        assert node_update.read(agent.root)["state"] == "failed"
    finally:
        agent.server_close()


@pytest.mark.parametrize("state", sorted(node_update.PENDING))
def test_node_helper_blocks_pending_updates_even_over_ssh(tmp_path, state):
    node_agent.load_tool()
    import hydraweave_agent_update
    root = node_agent.prepare(tmp_path / "node")
    node_update.write(root, {"state": state})
    with pytest.raises(ValueError, match="new work is refused"):
        hydraweave_agent_update.require_idle(root)


def test_interrupted_staging_is_recovered_but_active_staging_is_preserved(tmp_path):
    root = node_agent.prepare(tmp_path / "node")
    row = node_update.begin(root, "a" * 64, instance="old", listen="127.0.0.1", port=1,
                            lab_dir=None, announce=False, home=tmp_path)
    assert node_update.recover_staging(root)["state"] == "staging"
    row["stager"]["start_ticks"] += 1
    node_update.write(root, row)
    assert node_update.recover_staging(root)["state"] == "failed"


@pytest.fixture
def real_instances(tmp_path):
    """Fake systemctl switches real isolated Python/Agent HTTP processes; no systemd invocation."""
    root = node_agent.prepare(tmp_path / "node")
    (root / "etc/agent-pairing.json").write_text(json.dumps({"token": "fixture-token"}))
    candidate = root / "agent-releases/candidate"
    venv.EnvBuilder(with_pip=False).create(candidate)
    site = candidate / "lib" / ("python%d.%d" % sys.version_info[:2]) / "site-packages"
    shutil.copytree(ROOT / "hydraweave", site / "hydraweave", ignore=shutil.ignore_patterns("__pycache__"))
    metadata = site / ("hydraweave-" + __version__ + ".dist-info")
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: hydraweave\nVersion: " + __version__ + "\n")
    share = candidate / "share/hydraweave"
    shutil.copytree(ROOT / "node", share / "node", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(ROOT / "versions.lock", share / "versions.lock")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    # The existing environment's package is source-mounted; candidate is installed
    # separately and invoked with -I to exclude this checkout from import search.
    source = ("from hydraweave.node_agent import Agent; "
              f"agent=Agent(('127.0.0.1',{port}),{str(root)!r},record={{'address':'127.0.0.1'}}); "
              "agent.serve_forever()")
    processes, current = [], [None]
    row = {"listen": "127.0.0.1", "port": port}
    def start(updated=False, transaction=""):
        if current[0] is not None:
            current[0].terminate()
            current[0].wait(timeout=5)
        interpreter = str(candidate / "bin/python") if updated else sys.executable
        env = {**os.environ, "HYDRAWEAVE_AGENT_UPDATE": transaction}
        argv = [interpreter, "-I", "-c", source] if updated else [interpreter, "-c", source]
        process = subprocess.Popen(argv, env=env, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        processes.append(process)
        current[0] = process
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                return node_update.health(root, row)
            except OSError:
                if process.poll() is not None:
                    pytest.fail(process.stderr.read().decode())
                time.sleep(0.02)
        pytest.fail("fixture agent did not start")
    old = start()
    row = node_update.begin(root, "a" * 64, instance=old["instance"], listen="127.0.0.1", port=port,
                            lab_dir=None, announce=False, home=tmp_path / "home")
    row = node_update.write(root, row, candidate=str(candidate), state="prepared", version=__version__,
                            commit=discovery.controller_commit())
    try:
        yield SimpleNamespace(root=root, row=row, start=start, current=current, candidate=candidate, processes=processes)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stderr.close()


def test_activation_success_requires_new_authenticated_instance(real_instances):
    fixture = real_instances
    def command(argv, **kwargs):
        if "restart" in argv:
            fixture.start(True, fixture.row["id"])
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    result = node_update.apply(fixture.root, fixture.row["id"], runner=command, timeout=2, idle=lambda: None)
    assert result["state"] == "ready", result
    health = node_update.health(fixture.root, fixture.row)
    assert health["prefix"] == str(fixture.candidate)
    assert health["instance"] != fixture.row["previous_instance"]
    assert health["transaction"] == fixture.row["id"]
    assert fixture.candidate.is_dir()


def test_wrong_running_instance_rolls_back_and_verifies_restored_release(real_instances):
    fixture = real_instances
    destination = Path(fixture.row["dropin"])
    destination.parent.mkdir(parents=True)
    original = b"[Service]\nEnvironment=FIXTURE_RETAINED=yes\n"
    destination.write_bytes(original)
    import base64
    fixture.row = node_update.write(fixture.root, fixture.row,
                                    previous_dropin=base64.b64encode(original).decode())
    restarts = []
    def command(argv, **kwargs):
        if "restart" in argv:
            restarts.append(True)
            # A service override selected the wrong environment on activation.
            fixture.start(False, fixture.row["id"] if len(restarts) == 1 else "")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    result = node_update.apply(fixture.root, fixture.row["id"], runner=command, timeout=0.2, idle=lambda: None)
    assert result["state"] == "rolled_back", result
    assert len(restarts) == 2
    assert destination.read_bytes() == original
    assert fixture.candidate.is_dir()  # Candidate is retained for diagnostics; old release was never changed.
    assert node_update.health(fixture.root, fixture.row)["prefix"] == fixture.row["previous_prefix"]


def test_same_instance_cannot_satisfy_new_release_health(tmp_path):
    row = {"candidate": str(tmp_path), "previous_instance": "old", "id": "id", "version": "1", "commit": ""}
    with pytest.raises(ValueError, match="not confirmed"):
        node_update._wait_health(tmp_path, row, timeout=0, probe=lambda *_: {
            "prefix": str(tmp_path), "instance": "old", "transaction": "id", "version": "1"})


def test_active_storage_owner_refuses_update_without_scheduling(tmp_path):
    agent, tool, payload = agent_at(tmp_path)
    import hydraweave_model_jobs
    descriptor = hydraweave_model_jobs._lock(agent.root)
    try:
        with patch.object(node_agent, "load_tool", return_value=tool), pytest.raises(ValueError, match="owns this node"):
            agent.invoke("agent-update", payload)
        agent.runner.assert_not_called()
        agent.download.assert_not_called()
    finally:
        os.close(descriptor)
        agent.server_close()


def test_queued_background_job_blocks_update_before_worker_takes_lock(tmp_path):
    agent, tool, payload = agent_at(tmp_path)
    (agent.root / "run" / ("job-" + "b" * 32 + ".json")).write_text('{"state":"queued","kind":"download"}')
    try:
        with patch.object(node_agent, "load_tool", return_value=tool), pytest.raises(ValueError, match="background node job"):
            agent.invoke("agent-update", payload)
        agent.runner.assert_not_called()
    finally:
        agent.server_close()


def test_installer_cannot_publish_a_background_job_during_pending_update(tmp_path):
    tool = node_agent.load_tool()
    install = tool.extension_for("install")
    root = node_agent.prepare(tmp_path / "node")
    node_update.write(root, {"state": "staging"})
    with patch.object(install, "envelope", return_value=["nice", "-n", "19"]), \
            patch.object(install.subprocess, "Popen") as launch, pytest.raises(ValueError, match="new work is refused"):
        install.launch_job(root, "install", {})
    launch.assert_not_called()
    assert not list((root / "run").glob("job-*.json"))


def test_idle_refusal_does_not_restart_or_reconfigure_existing_agent(tmp_path):
    root = node_agent.prepare(tmp_path / "node")
    row = node_update.begin(root, "a" * 64, instance="old", listen="127.0.0.1", port=1,
                            lab_dir=None, announce=False, home=tmp_path)
    row = node_update.write(root, row, state="prepared", candidate=str(root / "candidate"), version="1", commit="")
    runner = Mock()
    def busy():
        raise ValueError("live background work")
    result = node_update.apply(root, row["id"], runner=runner, idle=busy)
    assert result["state"] == "failed"
    runner.assert_not_called()
    assert not Path(row["dropin"]).exists()


@pytest.mark.parametrize("character", ["$", "%", "\n"])
def test_systemd_expanding_paths_are_refused_before_any_scheduled_process(tmp_path, character):
    root = node_agent.prepare(tmp_path / ("node" + character + "path"))
    with pytest.raises(ValueError, match="unsupported character"):
        node_update.begin(root, "a" * 64, instance="old", listen="127.0.0.1", port=1,
                          lab_dir=None, announce=False, home=tmp_path)
    assert node_update.read(root) == {"state": "idle"}


def test_failed_rollback_retains_exclusion_and_does_not_report_ready(tmp_path):
    root = node_agent.prepare(tmp_path / "node")
    row = node_update.begin(root, "a" * 64, instance="old", listen="127.0.0.1", port=1,
                            lab_dir=None, announce=False, home=tmp_path)
    row = node_update.write(root, row, state="prepared", candidate=str(root / "candidate"), version="1", commit="")
    runner = Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    result = node_update.apply(root, row["id"], runner=runner, probe=lambda *_: {}, timeout=0, idle=lambda: None)
    assert result["state"] == "rollback_failed"
    assert "rollback" in result["error"]


def test_late_stager_cannot_overwrite_supervisor_timeout(tmp_path):
    agent, tool, payload = agent_at(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = agent.runner.side_effect
    errors = []
    def commands(argv, **kwargs):
        result = original(argv, **kwargs)
        if "-c" in argv:
            entered.set()
            assert release.wait(5)
        return result
    agent.runner.side_effect = commands
    def update():
        try:
            agent.invoke("agent-update", payload)
        except Exception as error:
            errors.append(error)
    try:
        with patch.object(node_agent, "load_tool", return_value=tool):
            worker = threading.Thread(target=update)
            worker.start()
            assert entered.wait(5)
            row = node_update.read(agent.root)
            result = node_update.apply(agent.root, row["id"], stage_timeout=0, idle=lambda: None)
            assert result["state"] == "failed"
            release.set()
            worker.join(5)
        assert errors and "state changed" in str(errors[0])
        assert node_update.read(agent.root)["state"] == "failed"
        assert not list((agent.root / "agent-releases").iterdir())
    finally:
        release.set()
        agent.server_close()


def test_authenticated_health_does_not_follow_redirects(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.error import HTTPError
    hits = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            hits.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/token-must-not-arrive")
            self.send_header("Content-Length", "0")
            self.end_headers()
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
    root = node_agent.prepare(tmp_path / "node")
    (root / "etc/agent-pairing.json").write_text('{"token":"private-fixture"}')
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with pytest.raises(HTTPError):
            node_update.health(root, {"listen": "127.0.0.1", "port": server.server_port})
        assert hits == ["/call"]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)


@pytest.mark.parametrize("bad_candidate", [False, True])
def test_bootstrap_migrates_legacy_protocol_and_verifies_real_rollback(real_instances, monkeypatch, bad_candidate):
    fixture = real_instances
    legacy = fixture.root / "agent-venv"
    venv.EnvBuilder(with_pip=False).create(legacy)
    package = legacy / "lib" / ("python%d.%d" % sys.version_info[:2]) / "site-packages/hydraweave"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    # The old protocol has authenticated version but no agent-health endpoint.
    (package / "node_agent.py").write_text('''import argparse,json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
p=argparse.ArgumentParser();p.add_argument('operation');p.add_argument('--root');p.add_argument('--listen');p.add_argument('--port',type=int);p.add_argument('--no-announce',action='store_true');a=p.parse_args()
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  valid=self.headers.get('Authorization')=='Bearer fixture-token' and body.get('verb')=='version'
  result={'schema':'hydraweave/node-v1','verb':'version','checks':{'version':'legacy','tool':'retained-digest'}} if valid else {'error':'unknown verb'}
  raw=json.dumps(result).encode();self.send_response(200 if valid else 400);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
ThreadingHTTPServer((a.listen,a.port),Handler).serve_forever()
''')
    def legacy_start():
        if fixture.current[0] is not None and fixture.current[0].poll() is None:
            fixture.current[0].terminate()
            fixture.current[0].wait(timeout=5)
        process = subprocess.Popen([str(legacy / "bin/python"), "-I", "-m", "hydraweave.node_agent", "serve",
                                    "--root", str(fixture.root), "--listen", "127.0.0.1", "--port",
                                    str(fixture.row["port"]), "--no-announce"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        fixture.processes.append(process)
        fixture.current[0] = process
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                return node_update._request(fixture.root, fixture.row, legacy=True)
            except OSError:
                time.sleep(0.02)
        pytest.fail("legacy fixture did not start")
    old_reply = legacy_start()
    assert old_reply["checks"]["version"] == "legacy"
    node_update.write(fixture.root, fixture.row, state="failed")
    monkeypatch.setattr(node_update.sys, "prefix", str(fixture.candidate))
    monkeypatch.setattr(node_update, "dropin_path", lambda home=None: Path(fixture.row["dropin"]))
    commands = []
    def command(argv, **kwargs):
        commands.append(argv)
        if "show" in argv:
            return SimpleNamespace(returncode=0, stdout=str(fixture.current[0].pid), stderr="")
        if "stop" in argv:
            fixture.current[0].terminate()
            fixture.current[0].wait(timeout=5)
        if "restart" in argv:
            row = node_update.read(fixture.root)
            if Path(row["dropin"]).exists():
                fixture.start(True, "wrong-transaction" if bad_candidate else row["id"])
            else:
                legacy_start()
        if argv[0] == "systemd-run":
            row = node_update.read(fixture.root)
            # Execute production activation; systemd itself is the fixture boundary.
            node_update.apply(fixture.root, row["id"], runner=command, timeout=0.2, idle=lambda: None)
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    if bad_candidate:
        with pytest.raises(ValueError, match="rolled_back"):
            node_update.migrate(fixture.root, "b" * 64, runner=command)
        assert node_update.public(fixture.root)["state"] == "rolled_back"
        assert node_update._request(fixture.root, fixture.row, legacy=True) == old_reply
        assert not Path(fixture.row["dropin"]).exists()
    else:
        result = node_update.migrate(fixture.root, "b" * 64, runner=command)
        assert result["state"] == "ready"
        assert node_update.health(fixture.root, fixture.row)["prefix"] == str(fixture.candidate)
    assert (legacy / "bin/python").is_file()
    assert not any("pip" in argv for argv in commands)
    assert any("stop" in argv for argv in commands)
