"""Staged node-agent releases, supervised activation and verified rollback.

The running agent never installs over its own imports. An independent user
service activates a prepared environment and verifies the authenticated process
that actually owns the agent listener before recording success.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from . import secrets as storage
from .procs import InstanceLock, LocalChild, port_owned, process_identity
from .winproc import no_window

PENDING = {"staging", "staged", "prepared", "activating", "rolling_back", "rollback_failed"}
TERMINAL = {"ready", "rolled_back", "failed"}
UNIT = "hydraweave-node.service"
DROPIN = "50-hydraweave-update.conf"


def state_path(root):
    return Path(root) / "run" / "agent-update.json"


def read(root):
    path = state_path(root)
    if path.is_symlink():
        raise ValueError("agent update receipt must not be a symlink")
    if not path.exists():
        return {"state": "idle"}
    with path.open(encoding="utf-8") as stream:
        raw = stream.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        raise ValueError("agent update receipt is too large")
    row = json.loads(raw)
    if not isinstance(row, dict) or row.get("state") not in PENDING | TERMINAL:
        raise ValueError("invalid agent update receipt")
    return row


def write(root, row, **changes):
    row = {**row, **changes, "updated_at": time.time()}
    storage._write(state_path(root), json.dumps(row, sort_keys=True).encode())
    return row


def transition(root, row, expected, *, staging=False, **changes):
    """Compare-and-publish: a late stager cannot undo a supervisor failure."""
    with root_lock(root):
        current = read(root)
        if current.get("id") != row.get("id") or current["state"] not in set(expected):
            raise ValueError("agent update state changed before publication")
        if staging and time.time() >= current["stage_deadline"]:
            write(root, current, state="failed", error="staging deadline expired before activation")
            raise ValueError("agent update staging deadline expired")
        return write(root, current, **changes)


def fail_staging(root, row, error):
    with root_lock(root):
        current = read(root)
        if current.get("id") == row.get("id") and current["state"] in {"staging", "staged"}:
            write(root, current, state="failed", error=str(error)[:1000])


def public(root):
    row = read(root)
    # Paths, previous service contents and pairing credentials stay on the node.
    return {key: row[key] for key in ("id", "state", "sha256", "version", "commit", "error", "updated_at")
            if key in row}


@contextmanager
def root_lock(root):
    """The same lock that publishes node lease/service records."""
    import fcntl
    filename = Path(root) / "run" / "root.lock"
    descriptor = os.open(filename, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "r+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def _run(argv, runner=None, *, timeout=1800):
    if runner is not None:
        result = runner(argv, capture_output=True, text=True, timeout=timeout, **no_window())
    else:
        # pip/build helpers must die with the stager, including after a timeout
        # or a leader exit. subprocess.run only owns the immediate process.
        executable = shutil.which(str(argv[0]))
        if executable is None:
            raise FileNotFoundError(str(argv[0]))
        deadline = time.monotonic() + timeout
        with tempfile.TemporaryDirectory(prefix="hydraweave-update-command-") as directory:
            child = LocalChild([str(Path(executable).absolute()), *argv[1:]], os.environ,
                               directory, "command", max_log_bytes=128 * 1024, log_backups=1)
            try:
                child.start()
                child.process.wait(timeout=max(0, deadline - time.monotonic()))
            finally:
                child.stop(grace_s=0.5)
            def output(label):
                path = Path(directory) / ("command." + label + ".log")
                parts = [path.with_name(path.name + ".1"), path]
                return b"".join(part.read_bytes() for part in parts if part.exists()).decode("utf-8", "replace")
            result = subprocess.CompletedProcess(argv, child.process.returncode, output("stdout"), output("stderr"))
    if result.returncode:
        detail = getattr(result, "stderr", "") or getattr(result, "stdout", "") or "command failed"
        raise ValueError("Node update failed: " + detail[-1000:])
    return result


def require_quiet(root, lab_dir=None, *, call=None):
    """Under root_lock: exclude GPU leases and all owned background node jobs."""
    from .node_agent import dispatch, load_tool
    install = load_tool().extension_for("install")
    if install.active_leases(root):
        raise ValueError("Update node refused: a live GPU lease is active; stop it first")
    import hydraweave_model_jobs
    hydraweave_model_jobs.refuse_storage_owner(root)
    for path in sorted((Path(root) / "run").glob("job-*.json")):
        if not re.fullmatch(r"job-[0-9a-f]{32}\.json", path.name):
            continue
        if path.is_symlink() or path.stat().st_size > 256 * 1024:
            raise ValueError("Update node refused: background job state is unverifiable")
        row = json.loads(path.read_text())
        if not isinstance(row, dict) or row.get("state") not in {"succeeded", "failed", "cancelled", "stopped"}:
            raise ValueError("Update node refused: a background node job is active or unconfirmed")
    if lab_dir:
        turns = (call or dispatch)("turns", {"lab_dir": lab_dir})
        holder = turns.get("holder")
        if turns.get("error") or (holder and holder.get("alive") is not False):
            raise ValueError("Update node refused: a GPU turn is live or its state cannot be verified")
        if not turns.get("present"):
            raise ValueError("Update node refused: GPU turn state is unavailable")


def _quote(value):
    value = str(value)
    if not value or any(char in value for char in '\n\r"%$\\\0'):
        raise ValueError("unsupported character in systemd update path")
    return '"' + value + '"'


def dropin_path(home=None):
    return Path(home or Path.home()) / ".config/systemd/user" / (UNIT + ".d") / DROPIN


def _checked_dropin(path):
    path = Path(path)
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError("agent service drop-in must not use symlinks")
    if path.exists() and (not path.is_file() or path.stat().st_size > 64 * 1024):
        raise ValueError("invalid agent service drop-in")
    return path


def _configuration(row):
    candidate = Path(row["candidate"])
    argv = [candidate / "bin/python", "-I", "-m", "hydraweave.node_agent", "serve",
            "--root", row["root"], "--listen", row["listen"], "--port", str(row["port"])]
    if row.get("lab_dir"):
        argv += ["--lab-dir", row["lab_dir"]]
    if not row.get("announce"):
        argv += ["--no-announce"]
    return ("[Service]\nExecStart=\nExecStart=" + " ".join(_quote(part) for part in argv)
            + "\nEnvironment=" + _quote("HYDRAWEAVE_AGENT_UPDATE=" + row["id"])
            + " " + _quote("PATH=" + str(candidate / "bin") + ":/usr/local/bin:/usr/bin:/bin") + "\n")


def begin(root, digest, *, instance, listen, port, lab_dir, announce, home=None):
    """Called under root_lock after proving there are no leases/new mutations."""
    for value in (root, Path(sys.executable).absolute(), listen, str(port), *( [lab_dir] if lab_dir else [] )):
        _quote(value)  # systemd-run also expands arguments; validate before scheduling/staging.
    if read(root).get("state") in PENDING:
        raise ValueError("Node update already in progress or awaiting rollback recovery")
    destination = _checked_dropin(dropin_path(home))
    previous = base64.b64encode(destination.read_bytes()).decode("ascii") if destination.exists() else None
    row = {"id": secrets.token_hex(16), "state": "staging", "sha256": digest,
           "root": str(root), "listen": listen, "port": port, "lab_dir": lab_dir, "announce": bool(announce),
           "previous_instance": instance, "previous_prefix": str(Path(sys.prefix).resolve()),
           "stage_deadline": time.time() + 1500,
           "stager": process_identity(os.getpid()), "dropin": str(destination), "previous_dropin": previous}
    return write(root, row)


def stage(root, row, data, *, runner=None):
    if hashlib.sha256(data).hexdigest() != row["sha256"]:
        raise ValueError("Node package SHA256 mismatch; nothing was installed")
    releases = Path(root) / "agent-releases"
    if releases.is_symlink():
        raise ValueError("agent release directory must not be a symlink")
    releases.mkdir(exist_ok=True)
    candidate = releases / row["id"]
    # A unique immutable pathname is essential: venv scripts embed their path.
    candidate.mkdir(mode=0o700)
    def command(argv, timeout):
        remaining = row["stage_deadline"] - time.time()
        if remaining <= 0:
            raise ValueError("agent update staging deadline expired")
        return _run(argv, runner, timeout=min(timeout, remaining))
    try:
        row = transition(root, row, {"staging"}, staging=True, candidate=str(candidate))
        command([str(Path(sys.executable).absolute()), "-m", "venv", str(candidate)], 120)
        python = candidate / "bin/python"
        with tempfile.TemporaryDirectory(prefix="agent-package-", dir=Path(root) / "incoming") as temporary:
            package = Path(temporary) / "package.tar.gz"
            package.write_bytes(data)
            command([str(python), "-I", "-m", "pip", "install", "--upgrade",
                     "hydraweave[node] @ " + package.as_uri()], 1200)
        command([str(python), "-I", "-m", "pip", "check"], 60)
        code = ("import json,sys; from pathlib import Path; from hydraweave import __version__,discovery,node_agent,node_update; "
                "node_agent.load_tool(); print(json.dumps({'version':__version__,'commit':discovery.controller_commit(),"
                "'prefix':str(Path(sys.prefix).resolve()),'module':node_agent.__file__}))")
        result = command([str(python), "-I", "-c", code], 60)
        health = json.loads(result.stdout)
        if (not isinstance(health.get("version"), str) or not health["version"]
                or health.get("prefix") != str(candidate.resolve())
                or not Path(health.get("module", "")).resolve().is_relative_to(candidate.resolve())):
            raise ValueError("staged node package loaded outside its candidate environment")
        row = transition(root, row, {"staging"}, staging=True,
                         version=health["version"], commit=health.get("commit", ""), state="staged")
        _configuration(row)  # Validate every systemd argument before scheduling activation.
        return row
    except BaseException:
        shutil.rmtree(candidate)  # Only the unique directory created above; never the active environment.
        raise


def schedule(root, row, *, runner=None):
    # This process survives the agent's own systemd restart. Restarting the
    # supervisor after a crash resumes rollback, never blindly repeats activation.
    _quote(root)
    _quote(Path(sys.executable).absolute())
    if not re.fullmatch(r"[0-9a-f]{32}", row.get("id", "")):
        raise ValueError("invalid update transaction")
    _run(["systemd-run", "--user", "--collect", "--on-active=2s", "--property=Type=exec", "--property=Restart=on-failure",
          "--property=RestartSec=1s", "--unit=hydraweave-node-update-" + row["id"], "--",
          str(Path(sys.executable).absolute()), "-I", "-m", "hydraweave.node_update",
          "--root", str(root), "--transaction", row["id"]], runner, timeout=30)


def _request(root, row, *, legacy=False):
    credentials = Path(root) / "etc/agent-pairing.json"
    if credentials.is_symlink():
        raise ValueError("invalid agent pairing file")
    token = json.loads(credentials.read_text())["token"] if credentials.exists() else None
    if not token and not legacy:
        raise ValueError("paired node credential is required for update health")
    host = row["listen"]
    if host in {"0.0.0.0", "::"}:
        host = "::1" if ":" in host else "127.0.0.1"
    if ":" in host:
        host = "[" + host + "]"
    endpoint = "/pair-code" if not token else "/call"
    body = {} if not token else {"verb": "version" if legacy else "agent-health", "payload": {}}
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(f"http://{host}:{row['port']}{endpoint}", json.dumps(body).encode(), headers)
    with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=2) as response:
        raw = response.read(16385)
    if len(raw) > 16384:
        raise ValueError("agent health reply is too large")
    return json.loads(raw)


def health(root, row):
    return _request(root, row)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # A node pairing token must never follow another endpoint/origin.


def _wait_health(root, row, *, restored=False, probe=None, timeout=30, runner=None):
    deadline = time.monotonic() + timeout
    while True:
        try:
            if restored and row.get("previous_legacy") and probe is None:
                return legacy_health(root, row, runner=runner)
            if row.get("migration") and not row["previous_legacy"]["paired"] and probe is None:
                current = service_snapshot(root, runner=runner)
                if (current["prefix"] != row["candidate"]
                        or current["identity"] == row["previous_legacy"]["identity"]):
                    raise ValueError("new unpaired service process identity is unconfirmed")
                reply = _request(root, current, legacy=True)
                if (not isinstance(reply.get("code"), str) or not re.fullmatch(r"\d{8}", reply["code"])
                        or process_identity(current["identity"]["pid"]) != current["identity"]):
                    raise ValueError("new unpaired service listener did not confirm readiness")
                return {"instance": "local:" + str(current["identity"]["pid"]) + ":" + str(current["identity"]["start_ticks"])}
            reply = (probe or health)(root, row)
            prefix = row["previous_prefix"] if restored else row["candidate"]
            if (reply.get("prefix") == str(Path(prefix).resolve())
                    and reply.get("instance") != row["previous_instance"]
                    and isinstance(reply.get("instance"), str)
                    and (restored or (reply.get("transaction") == row["id"]
                                      and reply.get("version") == row["version"]
                                      and reply.get("commit", "") == row.get("commit", "")))):
                return reply
        except (OSError, ValueError, KeyError):
            pass
        if time.monotonic() >= deadline:
            raise ValueError("restored agent health not confirmed" if restored else "new agent health not confirmed")
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))


def _restore(row):
    destination = _checked_dropin(row["dropin"])
    if row["previous_dropin"] is None:
        destination.unlink(missing_ok=True)
    else:
        storage._write(destination, base64.b64decode(row["previous_dropin"], validate=True))


def recover_staging(root):
    """A crashed stager has not activated anything; unblock the retained agent."""
    row = read(root)
    if row.get("state") not in {"staging", "staged"}:
        return row  # Startup/readiness must not wait for the activation root lock.
    with root_lock(root):
        row = read(root)
        if row.get("state") in {"staging", "staged"}:
            identity = row.get("stager")
            if not isinstance(identity, dict) or process_identity(identity.get("pid")) != identity:
                return write(root, row, state="failed", error="staging process ended before activation")
        return row


def apply(root, transaction, *, runner=None, probe=None, timeout=30, idle=None, stage_timeout=1900):
    """Independent activation; a restarted supervisor conservatively rolls back."""
    with closing(InstanceLock(Path(root) / "run/agent-update-worker.lock").acquire()):
        row = read(root)
        if row.get("id") != transaction:
            raise ValueError("agent update transaction changed")
        deadline = time.monotonic() + stage_timeout
        while row["state"] in {"staging", "staged"}:
            row = recover_staging(root)
            if row["state"] not in {"staging", "staged"}:
                break
            if time.monotonic() >= deadline or time.time() >= row["stage_deadline"]:
                try:
                    transition(root, row, {"staging", "staged"}, state="failed",
                               error="staging deadline expired before activation")
                except ValueError:
                    continue  # The stager published prepared under the same lock.
                return public(root)
            time.sleep(0.2)
            row = read(root)
            if row.get("id") != transaction:
                raise ValueError("agent update transaction changed")
        if row["state"] in TERMINAL:
            return public(root)
        if row["state"] not in {"prepared", "activating", "rolling_back", "rollback_failed"}:
            raise ValueError("agent update is not prepared")
        with root_lock(root):
            return _activate(root, row, runner=runner, probe=probe, timeout=timeout, idle=idle)


def _activate(root, row, *, runner=None, probe=None, timeout=30, idle=None):
    destination = _checked_dropin(row["dropin"])
    changed = row["state"] != "prepared"
    try:
        if row["state"] != "prepared":
            raise ValueError("activation supervisor restarted before a completed health receipt")
        if idle is not None:
            idle()
        else:
            require_quiet(Path(root), row.get("lab_dir"))
        row = write(root, row, state="activating")
        changed = True
        if row.get("migration"):
            # Legacy agents do not know the pending-update receipt. Stop
            # their listener under the lease publication lock, then recheck
            # leases before activating any new code. Their venv is retained.
            _run(["systemctl", "--user", "stop", UNIT], runner, timeout=60)
            if idle is not None:
                idle()
            else:
                require_quiet(Path(root), row.get("lab_dir"))
        storage._write(destination, _configuration(row).encode())
        _run(["systemctl", "--user", "daemon-reload"], runner, timeout=30)
        _run(["systemctl", "--user", "restart", UNIT], runner, timeout=60)
        reply = _wait_health(root, row, probe=probe, timeout=timeout, runner=runner)
        write(root, row, state="ready", instance=reply["instance"], error=None)
    except Exception as error:
        if not changed:
            write(root, row, state="failed", error=str(error)[:1000])
            return public(root)
        # No success state is published until the restored process is also
        # authenticated and its environment matches the retained release.
        row = write(root, row, state="rolling_back", error=str(error)[:1000],
                    rollback_attempts=row.get("rollback_attempts", 0) + 1)
        try:
            _restore(row)
            _run(["systemctl", "--user", "daemon-reload"], runner, timeout=30)
            _run(["systemctl", "--user", "restart", UNIT], runner, timeout=60)
            reply = _wait_health(root, row, restored=True, probe=probe, timeout=timeout, runner=runner)
            write(root, row, state="rolled_back", instance=reply["instance"])
        except Exception as rollback_error:
            write(root, row, state="rollback_failed",
                  error=(str(error) + "; rollback: " + str(rollback_error))[:1000])
    return public(root)

def service_snapshot(root, *, runner=None):
    """Local provenance for a pre-protocol bootstrap agent, never a bare PID."""
    result = _run(["systemctl", "--user", "show", UNIT, "--property=MainPID", "--value"], runner, timeout=5)
    pid = int(result.stdout.strip())
    identity = process_identity(pid)
    if not identity or (Path("/proc") / str(pid)).stat().st_uid != os.getuid():
        raise ValueError("cannot prove ownership of the running legacy node service")
    argv = [os.fsdecode(arg) for arg in (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0") if arg]
    if not argv or not Path(argv[0]).is_absolute() or Path(argv[0]).parent.name != "bin":
        raise ValueError("legacy agent is not running from an identifiable virtual environment")
    prefix = Path(argv[0]).parent.parent
    if prefix != Path(root) / "agent-venv" and not prefix.is_relative_to(Path(root) / "agent-releases"):
        raise ValueError("bootstrap migration requires a managed agent environment under the node root")
    entry = str(prefix / "bin/hydraweave-node") in argv or any(
        argv[index:index + 2] == ["-m", "hydraweave.node_agent"] for index in range(len(argv) - 1))
    def option(name, default):
        return argv[argv.index(name) + 1] if name in argv else default
    if not entry or option("--root", None) != str(root):
        raise ValueError("legacy service command does not match this node root")
    listen, port = option("--listen", "0.0.0.0"), int(option("--port", "18701"))
    if not port_owned(listen, port, [pid]) or process_identity(pid) != identity:
        raise ValueError("legacy agent does not own the configured listener")
    return {"identity": identity, "argv": argv, "prefix": str(prefix), "listen": listen, "port": port,
            "lab_dir": option("--lab-dir", None), "announce": "--no-announce" not in argv}


def legacy_health(root, row, *, runner=None):
    previous = row["previous_legacy"]
    current = service_snapshot(root, runner=runner)
    if (current["prefix"] != previous["prefix"] or current["argv"] != previous["argv"]
            or current["identity"] == previous["identity"]):
        raise ValueError("restored legacy service process identity is unconfirmed")
    reply = _request(root, current, legacy=True)
    if previous.get("paired"):
        if reply != previous["version_response"]:
            raise ValueError("restored legacy version differs from the retained release")
    elif not isinstance(reply.get("code"), str) or not re.fullmatch(r"\d{8}", reply["code"]):
        raise ValueError("restored unpaired legacy listener did not answer its local readiness probe")
    if process_identity(current["identity"]["pid"]) != current["identity"]:
        raise ValueError("legacy process changed during its readiness probe")
    return {"instance": "legacy:" + str(current["identity"]["pid"]) + ":" + str(current["identity"]["start_ticks"])}


def migrate(root, digest, *, runner=None, wait_timeout=180):
    """The verified bootstrap runs this from its fresh, separately installed venv."""
    from . import __version__, discovery
    from .node_agent import load_tool
    candidate = Path(sys.prefix).absolute()
    if not candidate.is_relative_to(Path(root) / "agent-releases") or candidate.is_symlink():
        raise ValueError("bootstrap migration must run from a fresh agent-releases environment")
    load_tool()  # Validate the newly installed dispatcher's packaged resources.
    previous = service_snapshot(root, runner=runner)
    version_response = _request(root, previous, legacy=True)
    paired = (Path(root) / "etc/agent-pairing.json").is_file()
    if paired and (version_response.get("schema") != "hydraweave/node-v1" or version_response.get("verb") != "version"):
        raise ValueError("legacy node did not answer the authenticated version probe")
    previous.update(paired=paired, version_response=version_response if paired else None)
    if not paired and (not isinstance(version_response.get("code"), str)
                       or not re.fullmatch(r"\d{8}", version_response["code"])):
        raise ValueError("legacy unpaired service did not answer its local readiness probe")
    with root_lock(root):
        row = begin(root, digest, instance="legacy", listen=previous["listen"], port=previous["port"],
                    lab_dir=previous["lab_dir"], announce=previous["announce"])
        row = write(root, row, candidate=str(candidate), previous_prefix=previous["prefix"],
                    previous_legacy=previous, migration=True, version=__version__, commit=discovery.controller_commit(),
                    state="prepared", stager=process_identity(os.getpid()))
    try:
        schedule(root, row, runner=runner)
    except Exception as error:
        transition(root, row, {"prepared"}, state="failed", error=str(error)[:1000])
        raise
    deadline = time.monotonic() + wait_timeout
    while time.monotonic() < deadline:
        result = public(root)
        if result.get("id") != row["id"]:
            raise ValueError("bootstrap migration transaction changed")
        if result["state"] in TERMINAL | {"rollback_failed"}:
            if result["state"] != "ready":
                raise ValueError("bootstrap migration " + result["state"] + ": " + str(result.get("error", "")))
            return result
        time.sleep(0.2)
    raise ValueError("bootstrap migration remains supervised; inspect run/agent-update.json before retrying")


def wait_ready(root, *, runner=None, timeout=30):
    """Bootstrap readiness also covers a new, not-yet-paired installation."""
    from . import __version__
    deadline = time.monotonic() + timeout
    while True:
        try:
            current = service_snapshot(root, runner=runner)
            if Path(current["prefix"]).resolve() != Path(sys.prefix).resolve():
                raise ValueError("another agent environment is serving")
            paired = (Path(root) / "etc/agent-pairing.json").is_file()
            reply = _request(root, current, legacy=not paired)
            if paired:
                valid = (reply.get("prefix") == str(Path(sys.prefix).resolve())
                         and reply.get("version") == __version__ and reply.get("update_protocol") == 1)
            else:
                valid = isinstance(reply.get("code"), str) and re.fullmatch(r"\d{8}", reply["code"])
            if valid and process_identity(current["identity"]["pid"]) == current["identity"]:
                return {"state": "ready", "paired": paired, **({"pairing_code": reply["code"]} if not paired else {})}
        except (OSError, ValueError, KeyError):
            pass
        if time.monotonic() >= deadline:
            raise ValueError("bootstrap agent readiness is unconfirmed; inspect the node user service")
        time.sleep(0.2)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--transaction")
    parser.add_argument("--bootstrap", action="store_true")
    parser.add_argument("--wait-ready", action="store_true")
    parser.add_argument("--sha256")
    args = parser.parse_args(argv)
    from .node_agent import prepare
    root = prepare(args.root)
    if args.wait_ready:
        if args.bootstrap or args.transaction or args.sha256:
            parser.error("wait-ready takes only a root")
        result = wait_ready(root)
    elif args.bootstrap:
        if args.transaction or not re.fullmatch(r"[0-9a-f]{64}", args.sha256 or ""):
            parser.error("bootstrap migration requires only a package SHA256")
        result = migrate(root, args.sha256)
    else:
        if not re.fullmatch(r"[0-9a-f]{32}", args.transaction or ""):
            parser.error("invalid update transaction")
        result = apply(root, args.transaction)
    print(json.dumps(result))
    # A persistent service/configuration failure must not create an unlimited
    # restart loop. The blocked receipt remains available for explicit recovery.
    return 1 if result["state"] == "rollback_failed" and read(root).get("rollback_attempts", 0) < 3 else 0


if __name__ == "__main__":
    raise SystemExit(main())
