"""Persistent, unprivileged adapter around the existing node JSON dispatcher."""
from __future__ import annotations

import argparse
import base64
import errno
import hmac
import importlib.machinery
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from urllib.request import Request, urlopen
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, bind, discovery
from .paths import repo_root
from .winproc import no_window


def load_tool():
    directory = repo_root() / "node"
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    loader = importlib.machinery.SourceFileLoader("hydraweave_node_agent_tool", str(directory / "hydraweave-node"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def dispatch(verb, payload):
    tool = load_tool()
    if verb == "poll":
        return tool.poll(payload)
    if verb in {"version", "doctor", "gpu"}:
        return tool.Inspector(*tool.core_inputs(payload)).inspect(verb)
    module = tool.extension_for(verb)
    if module is None:
        raise ValueError("unknown node verb")
    return module.run(verb, payload)


def identity_of(root):
    source = root / "etc" / "agent-id"
    if source.is_symlink() or not source.is_file():
        raise ValueError("agent-id must be a regular file")
    with source.open(encoding="ascii") as stream:
        value = stream.read(64).strip()
    if not re.fullmatch(r"[0-9a-f]{24}", value):
        raise ValueError("invalid agent identity")
    return value


def metadata(root, lab_dir, address, port):
    tool = load_tool()
    inspector = tool.Inspector(lab_dir=lab_dir)
    gfx = inspector.gfx()
    arch = ",".join(row["target"] for row in (gfx.get("value") or [])) or "unknown"
    from hydraweave_usb4 import identity
    usb = identity()
    ram = inspector.ram()
    versions = []
    for manifest in sorted((root / "releases").glob("*/MANIFEST.json"))[:64]:
        versions.append(manifest.parent.name)
    return {"id": "node-" + identity_of(root),
            "agent_version": __version__, "hostname": socket.gethostname(),
            "gpu_arch": arch, "unified_memory": (ram.get("value") or {}).get("total_bytes", 0),
            "engine_version": ",".join(versions)[:200],
            **usb, "address": address, "port": port}


def prepare(root):
    """Upgrade the real node root in place; never replace etc, log or releases."""
    requested = Path(root).expanduser()
    if requested.is_symlink():
        raise ValueError("install root must not be a symlink")
    root = requested.resolve()
    if root == Path(root.anchor) or Path.home().resolve().is_relative_to(root):
        raise ValueError("install root must not contain HOME")
    for name in ("etc", "incoming", "lab-arm", "log", "releases", "run"):
        path = root / name
        if path.is_symlink():
            raise ValueError("agent directories must not be symlinks")
        path.mkdir(parents=True, exist_ok=True)
    identity = root / "etc" / "agent-id"
    if not identity.exists():
        with identity.open("x", encoding="ascii") as stream:
            stream.write(secrets.token_hex(12))
    identity_of(root)
    marker = root / ".hydraweave-root"
    if marker.is_symlink():
        raise ValueError("install root marker must not be a symlink")
    if not marker.exists():
        with marker.open("x", encoding="ascii") as stream:
            stream.write(secrets.token_hex(16) + "\n")
    return root


class Agent(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, root, *, lab_dir=None, record=None, call=dispatch):
        self.root, self.lab_dir, self.call = prepare(root), lab_dir, call
        policy = self.root / "etc" / "install-policy.json"
        if not policy.exists():
            from . import secrets as storage
            storage._write(policy, json.dumps({"lab_dir": lab_dir, "asset_paths": []}).encode(), replace=False)
        self.lock, self.failures = threading.Lock(), []
        self.code = f"{secrets.randbelow(100000000):08d}"
        self.code_expires = time.monotonic() + 600
        self.credentials = self.root / "etc" / "agent-pairing.json"
        self.token = json.loads(self.credentials.read_text())["token"] if self.credentials.exists() else None
        super().__init__(address, AgentHandler)
        if record is None:
            host = address[0] if address[0] != "0.0.0.0" else self.lan_address()
            record = metadata(self.root, lab_dir, host, self.server_port)
        self.record = dict(record, port=self.server_port)
        self.record.update(install_root=str(self.root), lab_dir=lab_dir, user=Path.home().name)
        self.transport, self.last_announce = None, time.monotonic()

    def service_actions(self):
        if self.transport and time.monotonic() - self.last_announce >= 25:
            self.last_announce = time.monotonic()
            row = metadata(self.root, self.lab_dir, self.record["address"], self.server_port)
            self.record.update(row)
            self.transport.refresh(row)

    @staticmethod
    def lan_address():
        # Ubuntu commonly maps its hostname only to 127.0.1.1. Ask the multicast route for its source
        # address without sending a datagram, rather than publishing that loopback mapping to the LAN.
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
                route.connect((discovery.GROUP, discovery.PORT))
                address = route.getsockname()[0]
                if not ipaddress.ip_address(address).is_loopback:
                    return address
        except OSError:
            pass
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            if not ipaddress.ip_address(item[4][0]).is_loopback:
                return item[4][0]
        return "127.0.0.1"

    def pair(self, code):
        with self.lock:
            now = time.monotonic()
            self.failures = [stamp for stamp in self.failures if now - stamp < 60]
            if (self.token or now > self.code_expires or len(self.failures) >= 5
                    or not isinstance(code, str) or not hmac.compare_digest(code, self.code)):
                if len(self.failures) < 64:
                    self.failures.append(now)
                raise PermissionError("pairing refused; check the node console (code expires after 10 minutes)")
            token = secrets.token_urlsafe(32)
            from . import secrets as storage
            storage._write(self.credentials, json.dumps({"token": token}).encode(), replace=False, exists_ok=False)
            self.token = token
            self.code = ""
            return {"token": self.token, "node": self.record}

    def reissue_code(self):
        with self.lock:
            if self.token:
                raise PermissionError("node is already paired")
            self.code = f"{secrets.randbelow(100000000):08d}"
            self.code_expires = time.monotonic() + 600
            self.failures.clear()
            return {"code": self.code, "expires_in": 600}

    def invoke(self, verb, payload):
        # A paired controller cannot redirect this daemon to another user's install or lab root.
        if payload.get("root", str(self.root)) != str(self.root):
            raise ValueError("wrong install root")
        if payload.get("lab_dir", self.lab_dir) != self.lab_dir:
            raise ValueError("wrong lab directory")
        if verb == "poll":
            for part in payload.get("parts", []):
                self.check_paths(part.get("payload", {}))
        self.check_paths(payload)
        if verb == "provision-prepare":
            tool = load_tool()
            install = tool.extension_for("install")
            # Bootstrap explicitly owns this known layout; checked_root still validates its policies.
            marker = self.root / ".hydraweave-root"
            if not marker.exists():
                with marker.open("x", encoding="ascii") as stream:
                    stream.write(secrets.token_hex(16) + "\n")
            install.checked_root({"root": str(self.root), "lab_dir": self.lab_dir}, create=True)
            required, free = 8 << 30, shutil.disk_usage(self.root).free
            if free < required:
                raise ValueError("insufficient disk space: engine provisioning requires at least 8 GiB free")
            base = self.root.parent / "hydraweave-models"
            for value in payload.get("directories", []):
                directory = Path(value)
                if (not directory.is_absolute() or not directory.is_relative_to(base)
                        or ".." in directory.parts or directory.resolve() != directory):
                    raise ValueError("only dedicated hydraweave-models subdirectories can be created")
                directory.mkdir(parents=True, exist_ok=True)
                marker = directory / ".hydraweave-assets"
                if marker.is_symlink() or (marker.exists() and not marker.is_file()):
                    raise ValueError("invalid asset directory marker")
                if not marker.exists():
                    with marker.open("x"):
                        pass
            return {"prepared": True, "disk": {"required_bytes": required, "free_bytes": free, "ok": True}}
        if verb == "bundle-chunk":
            install = load_tool().extension_for("bundle-prepare")
            stage = install.stage_path(self.root, payload)
            size, offset = payload["bytes"], payload["offset"]
            data = base64.b64decode(payload["data"], validate=True)
            if (type(size) is not int or not 0 < size <= install.MAX_BUNDLE
                    or type(offset) is not int or offset < 0 or len(data) > 128 * 1024 or offset + len(data) > size):
                raise ValueError("invalid bundle chunk")
            with self.lock:
                if shutil.disk_usage(stage.parent).free < size - offset + (1 << 30):
                    raise ValueError("insufficient disk space for source bundle")
                flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(stage, flags, 0o600)
                with os.fdopen(fd, "r+b") as stream:
                    stream.seek(0, os.SEEK_END)
                    if stream.tell() != offset:
                        raise ValueError("source bundle offset mismatch")
                    stream.write(data)
            return {"offset": offset + len(data)}
        return self.call(verb, payload)

    def check_paths(self, payload):
        if "root" in payload and payload["root"] != str(self.root):
            raise ValueError("wrong install root")
        if "lab_dir" in payload and payload["lab_dir"] != self.lab_dir:
            raise ValueError("wrong lab directory")


class AgentHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass                      # pairing codes and bearer tokens never enter access logs

    def do_POST(self):
        try:
            # The controller addresses agents by IP literal only; refusing every DNS name means a web page
            # cannot rebind its own name to this agent (the portal's Host rule, without its host names).
            hosts = self.headers.get_all("Host", [])
            if len(hosts) != 1 or not bind.host_allowed(hosts[0], "0.0.0.0", self.server.server_port, names=set()):
                raise PermissionError("Host is not allowed")
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 256 * 1024:
                raise ValueError("invalid body length")
            self.connection.settimeout(10)
            body = load_tool().decode_json(self.rfile.read(size).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("request must be a JSON object")
            if self.path == "/pair":
                result = self.server.pair(body.get("code"))
            elif self.path == "/pair-code":
                if not bind.is_loopback(self.client_address[0]):
                    raise PermissionError("pairing code can only be reissued locally")
                result = self.server.reissue_code()
            elif self.path == "/call":
                expected = "Bearer " + (self.server.token or secrets.token_urlsafe())
                if not self.server.token or not hmac.compare_digest(self.headers.get("Authorization", ""), expected):
                    raise PermissionError("pair this node first")
                result = self.server.invoke(body["verb"], body["payload"])
            else:
                raise ValueError("unknown endpoint")
            status = 200
        except PermissionError as exc:
            status, result = 403, {"error": str(exc)}
        except (ValueError, OSError, KeyError, TypeError) as exc:
            status, result = 400, {"error": str(exc)}
        raw = json.dumps(result).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def default_lab_dir():
    """Lab mode is opted into with HYDRAWEAVE_LAB_DIR (an existing directory); there is no guessed default."""
    value = os.environ.get("HYDRAWEAVE_LAB_DIR", "").strip()
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("HYDRAWEAVE_LAB_DIR must be an existing absolute directory")
    return str(path)


LEASE_TERMINAL = {"stopped", "failed", "aborted", "cancelled"}


def live_leases(root):
    """Labels of non-terminal lease records under root/run (engines started through the agent)."""
    labels = []
    for path in sorted((Path(root) / "run").glob("lease-*.json")):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(state, dict) and state.get("state") not in LEASE_TERMINAL:
            labels.append(str(state.get("label") or path.stem[len("lease-"):]))
    return labels


class LeaseLive(RuntimeError):
    pass


def install_service(root, lab_dir=None, *, no_announce=False, force=False, run=None):
    """Write and enable the agent unit; restart a running agent so it loads the new code.

    A restart is refused while a lease is live unless force: the unit uses KillMode=process,
    so engines survive, but the restarted agent would serve them with a different release.
    """
    run = run or subprocess.run
    root = prepare(root)
    status = run(["systemctl", "--user", "is-active", "--quiet", "hydraweave-node.service"], check=False, **no_window())
    active = getattr(status, "returncode", 1) == 0
    leases = live_leases(root) if active else []
    if leases and not force:
        raise LeaseLive("a lease is live (" + ", ".join(leases) + "); stop it or rerun install-service with --force")
    lab_dir = lab_dir or default_lab_dir()
    candidate = Path(sys.executable).with_name("hydraweave-node" + (".exe" if os.name == "nt" else ""))
    executable = str(candidate) if candidate.is_file() else (shutil.which("hydraweave-node") or str(candidate))
    values = [executable.replace("\\", "/"), root.as_posix(), (lab_dir or "").replace("\\", "/")]
    if any(any(char in value for char in '\n\r"%\\') for value in values):
        raise ValueError("unsupported character in systemd path")
    command = f'"{values[0]}" serve --root "{values[1]}"'
    if lab_dir:
        command += f' --lab-dir "{values[2]}"'
    if no_announce:
        command += " --no-announce"
    bin_dir = Path(sys.executable).parent.as_posix()
    unit = ("[Unit]\nDescription=Hydraweave discovery agent\nAfter=network-online.target\n\n"
            "[Service]\nExecStart=" + command + '\nEnvironment="PATH=' + bin_dir
            + ':/usr/local/bin:/usr/bin:/bin"\nRestart=on-failure\nRestartSec=5\nKillMode=process\nUMask=0077\n\n'
            "[Install]\nWantedBy=default.target\n")
    directory = Path.home() / ".config/systemd/user"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "hydraweave-node.service").write_text(unit)
    run(["systemctl", "--user", "daemon-reload"], check=True, **no_window())
    run(["systemctl", "--user", "enable", "--now", "hydraweave-node.service"], check=True, **no_window())
    if active:
        run(["systemctl", "--user", "restart", "hydraweave-node.service"], check=True, **no_window())
    try:
        run(["loginctl", "enable-linger", os.environ.get("USER", Path.home().name)], check=True, **no_window())
    except (OSError, subprocess.SubprocessError):
        print("For persistent service startup, run: loginctl enable-linger $USER")


def service_command(operation, *, run=None):
    run = run or subprocess.run
    if sys.platform == "win32":
        print("not supported, use the Windows installer")
        return 2
    unit = Path.home() / ".config/systemd/user/hydraweave.service"
    if operation == "install":
        command = f'"{Path(sys.executable).resolve()}" -m hydraweave ui --no-browser'
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text("[Unit]\nDescription=Hydraweave portal\n\n[Service]\nExecStart=" + command +
                        "\nRestart=on-failure\n\n[Install]\nWantedBy=default.target\n", encoding="utf-8")
        calls = [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", "hydraweave.service"]]
    elif operation in {"uninstall", "remove"}:
        calls = [["systemctl", "--user", "disable", "--now", "hydraweave.service"], ["systemctl", "--user", "daemon-reload"]]
        unit.unlink(missing_ok=True)
    elif operation in {"start", "stop"}:
        calls = [["systemctl", "--user", operation, "hydraweave.service"]]
    else:
        # systemctl status exits 3 for an inactive unit: report it, do not raise.
        return run(["systemctl", "--user", "status", "hydraweave.service"], check=False, **no_window()).returncode
    for call in calls:
        run(call, check=True, **no_window())
    if operation == "install":
        try:
            run(["loginctl", "enable-linger", os.environ.get("USER", Path.home().name)], check=True, **no_window())
        except (OSError, subprocess.SubprocessError):
            print("For persistent service startup, run: loginctl enable-linger $USER")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", nargs="?", choices=("serve", "install-service", "pair"))
    parser.add_argument("--root", default=str(Path.home() / "hydraweave"))
    parser.add_argument("--lab-dir")
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18701)
    parser.add_argument("--no-announce", action="store_true", default=os.environ.get("HYDRAWEAVE_NO_ANNOUNCE") == "1")
    parser.add_argument("--force", action="store_true", help="install-service: restart the agent even while a lease is live")
    args = parser.parse_args(argv)
    if args.operation is None:
        parser.print_help()
        return 0
    if sys.platform == "win32" and args.operation != "serve":
        print("not supported, use the Windows installer")
        return 0
    if args.operation == "pair":
        with urlopen(Request(f"http://127.0.0.1:{args.port}/pair-code", data=b"{}", headers={"Content-Type": "application/json"}), timeout=5) as response:
            print("Hydraweave pairing code: " + json.loads(response.read())["code"])
        return 0
    args.lab_dir = args.lab_dir or default_lab_dir()
    if args.operation == "install-service":
        try:
            install_service(args.root, args.lab_dir, no_announce=args.no_announce, force=args.force)
        except LeaseLive as error:
            print("hydraweave-node: " + str(error), file=sys.stderr)
            return 3
        return 0
    args.listen = bind.address(args.listen)
    root = prepare(args.root)
    try:
        server = Agent((args.listen, args.port), root, lab_dir=args.lab_dir)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE or args.port != 18701:
            raise
        server = Agent((args.listen, 0), root, lab_dir=args.lab_dir)
    print(f"Hydraweave agent port: {server.server_port}", flush=True)
    if not bind.is_loopback(args.listen):
        print(f"WARNING: the Hydraweave node agent listens on {bind.url_host(args.listen, server.server_port)} "
              "over plain HTTP: anyone on this network can try the console pairing code; once paired, the "
              "controller holding the token can run node operations. Use it only on a trusted LAN.",
              file=sys.stderr, flush=True)
    print("Hydraweave pairing code: " + (server.code if not server.token else "already paired"), flush=True)
    transport = None
    try:
        if not args.no_announce:
            # Local paths/user are obtained only after pairing, never advertised on the LAN.
            record = {k: v for k, v in server.record.items() if k not in {"install_root", "lab_dir", "user"}}
            transport = discovery.DNSService(discovery.Registry(), record)
            server.transport = transport
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if transport:
            transport.close()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
