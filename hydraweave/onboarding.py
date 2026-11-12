"""Discovery inventory and pairing-backed, merge-only controller adoption."""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
from urllib.request import ProxyHandler, Request, build_opener
from urllib.error import HTTPError

from . import config, discovery, nodegate


def request(address, port, endpoint, body, token=None, timeout=60):
    import ipaddress
    host = str(ipaddress.IPv4Address(address))
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = Request(f"http://{host}:{int(port)}/{endpoint}", json.dumps(body).encode(), headers)
    try:
        with build_opener(ProxyHandler({})).open(req, timeout=timeout) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
    except HTTPError as exc:
        message = json.loads(exc.read(4096)).get("error", "node refused request")
        raise PermissionError(message) from None
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("agent reply too large")
    result = json.loads(raw)
    if result.get("error"):
        raise ValueError(result["error"])
    return result


def _host(value):
    return (value or "").rsplit("@", 1)[-1].rstrip(".").lower().removesuffix(".local")


def match(raw, row, saved=None):
    saved = saved or {}
    hits = []
    for machine in raw["machines"]:
        previous = saved.get(machine["id"], {})
        hostname = _host(row.get("hostname"))     # empty names/addresses never match each other
        names = {_host(machine["id"]), _host(machine.get("ssh_target")), _host(machine.get("http_address"))} - {""}
        if (machine["id"] == row["id"] or previous.get("node_id") == row["id"]
                or (hostname and hostname in names)
                or (row.get("address") and machine.get("http_address") == row["address"])
                or set(previous.get("usb4_ids", [])) & set(row.get("usb4_ids", []))
                or set(previous.get("usb4_peers", [])) & set(row.get("usb4_peers", []))):
            hits.append(machine)
    if len(hits) > 1:
        raise ValueError("ambiguous node identity; fix duplicate addresses/hostnames before adoption")
    return hits[0] if hits else None


def credentials_path(path):
    return Path(path).parent / "node-agents.json"


def credentials(path):
    source = credentials_path(path)
    return json.loads(source.read_text()) if source.exists() else {}


def _save_credentials(path, value):
    from . import secrets
    secrets._write(credentials_path(path), json.dumps(value).encode())


class Controller:
    def __init__(self, path, *, registry=None):
        self.path, self.registry = Path(path), registry or discovery.Registry()
        self.transport = None
        self.error = None
        self.lock = threading.Lock()

    def start(self):
        if os.environ.get("HYDRAWEAVE_NO_DISCOVERY") != "1" and self.transport is None:
            try:
                self.transport = discovery.DNSService(self.registry)
            except OSError as exc:
                self.error = str(exc)
        return self

    def close(self):
        if self.transport:
            self.transport.close()
            self.transport = None

    def inventory(self):
        saved = credentials(self.path)
        raw = config.read_json(self.path) if self.path.exists() else {"machines": []}
        rows = self.registry.rows()
        for row in rows:
            try:
                machine = match(raw, row, saved)
                if machine:
                    row["machine"] = machine["id"]
                    if machine["id"] in saved:
                        row["state"] = "adopted"
            except ValueError as exc:
                row["error"] = str(exc)
        return {"nodes": rows, "pairs": discovery.pairs(rows), "error": self.error}

    def adopt(self, node_id, code):
        from .web.features import ops
        with self.lock, ops._write_lock, ops.ConfigLock(self.path):
            raw = config.read_json(self.path)
            saved = credentials(self.path)
            row = next((row for row in self.registry.rows() if row["id"] == node_id), None)
            if row is None:
                raise LookupError("node is no longer discovered")
            # Reject ambiguous matches before consuming the one-use pairing code.
            existing = match(raw, row, saved)
            if existing and existing["id"] in saved:
                return {"adopted": existing["id"], "merged": True, "already_adopted": True}
            response = request(row["address"], row["port"], "pair", {"code": code}, timeout=10)
            verified = response["node"]
            if verified["id"] != node_id:
                raise ValueError("node identity changed during pairing")
            verified.update(address=row["address"], port=row["port"])
            existing = match(raw, verified, saved)
            merged = existing is not None
            if existing:
                mid = existing["id"]       # Keep the existing ID, SSH routes, assets and GPU queue settings.
                if existing["install_root"] != verified["install_root"]:
                    raise ValueError("node install root differs from existing machine")
            else:
                mid = config.identifier(node_id, "node id")
                devices = verified.get("devices")
                if isinstance(devices, list) and devices and all(isinstance(d, dict) for d in devices):
                    gpus = 0
                    adopted = []
                    for device in devices:
                        kind = device.get("type") if device.get("type") in config.DEVICE_TYPES else "other"
                        if kind == "cpu":
                            did = "cpu"
                        else:
                            did, gpus = f"gpu{gpus}", gpus + 1
                        adopted.append({**device, "id": did, "type": kind})
                    devices = adopted
                else:
                    devices = [{"id": "gpu", "type": "amd"}]  # agents older than the pairing inventory
                node_os = verified.get("os") if verified.get("os") in ("linux", "windows", "darwin") else "linux"
                raw["machines"].append({"id": mid, "os": node_os, "access": "agent",
                                        "http_address": verified["address"], "install_root": verified["install_root"],
                                        "lab_mode": bool(verified.get("lab_dir")),
                                        **({"lab_dir": verified["lab_dir"]} if verified.get("lab_dir") else {}),
                                        "devices": devices})
            config.parse(raw)
            saved[mid] = {"node_id": node_id, "address": row["address"], "port": row["port"],
                          "token": response["token"], "usb4_ids": verified.get("usb4_ids", []),
                          "usb4_peers": verified.get("usb4_peers", [])}
            _save_credentials(self.path, saved)
            if not merged:
                ops.write_config(self.path, raw)
            nodegate.prepare(self.path, [mid])
            return {"adopted": mid, "merged": merged}


class Client:
    def __init__(self, machine, config_path):
        self.machine, self.config_path = machine, config_path

    def call(self, verb, payload, timeout=60, **kwargs):
        saved = credentials(self.config_path).get(self.machine.id)
        if not saved:
            raise ValueError("node has not been paired with this controller")
        return request(saved["address"], saved["port"], "call", {"verb": verb, "payload": payload},
                       saved["token"], timeout)

    def upload(self, local, remote_path, timeout=3600):
        import base64
        source = Path(local)
        size, offset = source.stat().st_size, 0
        with source.open("rb") as stream:
            while data := stream.read(128 * 1024):
                reply = self.call("bundle-chunk", {"root": self.machine.install_root, "stage": remote_path,
                                                   "bytes": size, "offset": offset,
                                                   "data": base64.b64encode(data).decode("ascii")}, timeout=timeout)
                offset = reply["offset"]


def of(api):
    with _controllers_lock:
        if getattr(api, "node_discovery", None) is None:
            api.node_discovery = Controller(api.config_path)
        return api.node_discovery


_controllers_lock = threading.Lock()
