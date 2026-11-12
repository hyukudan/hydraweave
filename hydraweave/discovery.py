"""Small DNS-SD transport. Announcements are hints, never authorization."""
from __future__ import annotations

import importlib
import ipaddress
import json
import logging
import re
import socket
import struct
import threading
import time

SERVICE = "_hydraweave._tcp.local."
GROUP = "224.0.0.251"
PORT = 5353
TTL = 90
LOG = logging.getLogger(__name__)
TXT_FIELDS = {"id", "agent_version", "agent_commit", "hostname", "gpu_arch", "unified_memory", "engine_version",
              "usb4_ids", "usb4_peers", "port"}


def properties(record):
    return {key: json.dumps(value, separators=(",", ":")).encode()
            for key, value in record.items() if key in TXT_FIELDS}


def from_properties(values, address, port):
    row = {}
    for key, value in values.items():
        key = key.decode() if isinstance(key, bytes) else key
        if key in TXT_FIELDS:
            row[key] = json.loads(value)
    if not isinstance(row.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", row["id"]):
        raise ValueError("invalid announcement id")
    for key in ("agent_version", "agent_commit", "hostname", "gpu_arch", "engine_version"):
        value = row.get(key, "")
        if not isinstance(value, str) or len(value) > 253 or any(ord(c) < 32 for c in value):
            raise ValueError("invalid announcement " + key)
    for key in ("usb4_ids", "usb4_peers"):
        value = row.get(key, [])
        if (not isinstance(value, list) or len(value) > 16
                or any(not isinstance(item, str) or not 1 <= len(item) <= 64 for item in value)):
            raise ValueError("invalid announcement " + key)
    memory = row.get("unified_memory", 0)
    if type(memory) is not int or not 0 <= memory <= 1 << 60:
        raise ValueError("invalid announcement unified_memory")
    ipaddress.IPv4Address(address)
    from .config import address as private_address
    private_address(address, "agent address")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("invalid agent port")
    row.update(address=address, port=port)
    return row


def controller_commit():
    from pathlib import Path
    try:
        return json.loads((Path(__file__).parent / "data" / "_build.json").read_text(encoding="utf-8"))["commit"]
    except (OSError, ValueError, KeyError):
        return ""


def version_mismatch(row):
    from . import __version__
    commit = controller_commit()
    return bool(row.get("agent_version") != __version__ or
                (commit and row.get("agent_commit") != commit))


class Registry:
    def __init__(self, clock=time.monotonic):
        self.clock, self.lock, self.records = clock, threading.Lock(), {}

    def update(self, row, ttl=TTL):
        with self.lock:
            if ttl:
                if row["id"] not in self.records and len(self.records) >= 128:
                    return
                self.records[row["id"]] = (self.clock() + min(ttl, TTL), dict(row))
            else:
                self.records.pop(row["id"], None)

    def rows(self):
        from . import __version__
        from .version import version_status
        with self.lock:
            self.records = {key: value for key, value in self.records.items() if value[0] > self.clock()}
            return [dict(value[1], state="discovered, not adopted",
                         version_status=version_status(value[1].get("agent_version"), __version__),
                         version_mismatch=version_mismatch(value[1]))
                    for value in self.records.values()]


def pairs(rows):
    """A peer UUID must match the other host's own USB4 UUID, not another peer's UUID."""
    result = []
    for index, first in enumerate(rows):
        for second in rows[index + 1:]:
            if (set(first.get("usb4_peers", [])) & set(second.get("usb4_ids", []))
                    and set(second.get("usb4_peers", [])) & set(first.get("usb4_ids", []))):
                result.append([first["id"], second["id"]])
    return result


def _name(value):
    return b"".join(bytes([len(part)]) + part.encode("ascii") for part in value.rstrip(".").split(".")) + b"\0"


def _read_name(packet, offset, seen=None):
    seen = set() if seen is None else seen
    labels, end = [], None
    while True:
        if offset in seen or offset >= len(packet):
            raise ValueError("invalid DNS name")
        seen.add(offset)
        size = packet[offset]
        offset += 1
        if size & 0xc0 == 0xc0:
            pointer = ((size & 0x3f) << 8) | packet[offset]
            suffix, _ = _read_name(packet, pointer, seen)
            labels.append(suffix.rstrip("."))
            end = offset + 1
            break
        if size > 63:
            raise ValueError("invalid DNS label")
        if not size:
            break
        labels.append(packet[offset:offset + size].decode("ascii"))
        offset += size
    return ".".join(labels) + ".", end if end is not None else offset


def packet(record, ttl=TTL):
    instance = record["id"] + "." + SERVICE
    host = record["id"] + ".local."
    txt = b""
    for key, value in properties(record).items():
        item = key.encode() + b"=" + value
        if len(item) > 255:
            raise ValueError("DNS-SD TXT field exceeds 255 bytes")
        txt += bytes([len(item)]) + item
    records = [(SERVICE, 12, _name(instance)),
               (instance, 33, struct.pack("!HHH", 0, 0, record["port"]) + _name(host)),
               (instance, 16, txt), (host, 1, socket.inet_aton(record["address"]))]
    return struct.pack("!6H", 0, 0x8400, 0, len(records), 0, 0) + b"".join(
        _name(name) + struct.pack("!HHIH", kind, 1, ttl, len(data)) + data for name, kind, data in records)


def receive(data, source, registry):
    """Parse bounded DNS packets, including compressed names from other DNS-SD stacks."""
    if len(data) > 8192:
        return False
    try:
        _, flags, questions, answers, authority, additional = struct.unpack_from("!6H", data)
        offset, entries, hosts = 12, {}, {}
        for _ in range(questions):
            _, offset = _read_name(data, offset)
            offset += 4
        for _ in range(answers + authority + additional):
            name, offset = _read_name(data, offset)
            kind, _, ttl, length = struct.unpack_from("!HHIH", data, offset)
            offset += 10
            end = offset + length
            if end > len(data):
                raise ValueError("truncated DNS record")
            if name.endswith("." + SERVICE):
                entry = entries.setdefault(name, {})
                if kind == 33:
                    entry["port"] = struct.unpack_from("!HHH", data, offset)[2]
                    entry["target"] = _read_name(data, offset + 6)[0].lower()
                elif kind == 16:
                    values, cursor = {}, offset
                    while cursor < end:
                        size = data[cursor]
                        if not size or cursor + 1 + size > end:
                            raise ValueError("invalid TXT record")
                        key, value = data[cursor + 1:cursor + 1 + size].split(b"=", 1)
                        values[key] = value
                        cursor += size + 1
                    entry.update(values=values, ttl=ttl)
            elif kind == 1 and length == 4:
                hosts[name.lower()] = socket.inet_ntoa(data[offset:end])
            offset = end
        for entry in entries.values():
            if "values" in entry and "port" in entry:
                # The datagram's source is authoritative. Only an announcement from this same host arrives from
                # loopback; then its own A record names the LAN address other machines must use.
                address = source
                if ipaddress.ip_address(source).is_loopback and entry.get("target") in hosts:
                    address = hosts[entry["target"]]
                registry.update(from_properties(entry["values"], address, entry["port"]), entry["ttl"])
        return not bool(flags & 0x8000) and _name(SERVICE) in data
    except (ValueError, TypeError, KeyError, IndexError, struct.error, UnicodeError, RecursionError):
        return False


class Multicast:
    def __init__(self, registry, record=None, *, interface="0.0.0.0", port=PORT, group=GROUP):
        self.registry, self.record = registry, record
        self.destination, self.stop_event = (group, port), threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind(("", port))
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                 socket.inet_aton(group) + socket.inet_aton(interface))
            if interface != "0.0.0.0":
                self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface))
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            self.sock.settimeout(0.25)
            self.thread = threading.Thread(target=self._run, name="hydraweave-mdns", daemon=True)
            self.thread.start()
        except BaseException:
            self.sock.close()
            raise

    def _send(self, data):
        self.sock.sendto(data, self.destination)

    def refresh(self, record):
        self.record = record
        self._send(packet(record))

    def _run(self):
        next_send = 0
        while not self.stop_event.is_set():
            try:
                if time.monotonic() >= next_send:
                    self._send(packet(self.record) if self.record else
                               struct.pack("!6H", 0, 0, 1, 0, 0, 0) + _name(SERVICE) + struct.pack("!HH", 12, 1))
                    next_send = time.monotonic() + 25
                data, source = self.sock.recvfrom(8193)
                query = receive(data, source[0], self.registry)
                if self.record and query:
                    self._send(packet(self.record))
            except socket.timeout:
                continue
            except OSError:
                if not self.stop_event.is_set():
                    LOG.exception("mDNS socket failed")
                break

    def close(self):
        self.stop_event.set()
        if self.record:
            try:
                self._send(packet(self.record, 0))
            except OSError:
                pass
        self.thread.join(timeout=1)
        self.sock.close()


class DNSService:
    def __init__(self, registry, record=None, *, backend=None):
        try:
            zc = backend or importlib.import_module("zeroconf")
        except ImportError:
            self.impl = Multicast(registry, record)
            return
        self.impl = self
        self.backend = zc
        self.zc = zc.Zeroconf(ip_version=zc.IPVersion.V4Only)
        self.info = None
        self.names, self.names_lock = set(), threading.Lock()
        self.stop_event = threading.Event()
        if record:
            self.info = zc.ServiceInfo(SERVICE, record["id"] + "." + SERVICE,
                                       addresses=[socket.inet_aton(record["address"])], port=record["port"],
                                       properties=properties(record), server=record["id"] + ".local.")
            self.zc.register_service(self.info)
        outer = self

        class Listener:
            def add_service(self, service, kind, name):
                self.update_service(service, kind, name)

            def update_service(self, service, kind, name):
                with outer.names_lock:
                    if len(outer.names) < 64:
                        outer.names.add(name)
                info = service.get_service_info(kind, name, timeout=1500)
                if info:
                    try:
                        registry.update(from_properties(info.properties, info.parsed_addresses()[0], info.port))
                    except (ValueError, TypeError, IndexError, UnicodeError, RecursionError):
                        LOG.warning("ignored invalid DNS-SD advertisement")

            def remove_service(self, service, kind, name):
                with outer.names_lock:
                    outer.names.discard(name)
                for row in registry.rows():
                    if name == row["id"] + "." + SERVICE:
                        registry.update(row, 0)

        self.listener = Listener()
        self.browser = zc.ServiceBrowser(outer.zc, SERVICE, self.listener)
        self.renew_thread = threading.Thread(target=self._renew, name="hydraweave-dnssd-cache", daemon=True)
        self.renew_thread.start()

    def _renew(self):
        while not self.stop_event.wait(20):
            with self.names_lock:
                names = list(self.names)
            for name in names:
                if self.stop_event.is_set():
                    return
                self.listener.update_service(self.zc, SERVICE, name)

    def refresh(self, record):
        if self.impl is not self:
            return self.impl.refresh(record)
        if self.info:
            self.info = self.backend.ServiceInfo(SERVICE, record["id"] + "." + SERVICE,
                                                 addresses=[socket.inet_aton(record["address"])], port=record["port"],
                                                 properties=properties(record), server=record["id"] + ".local.")
            self.zc.update_service(self.info)

    def close(self):
        if self.impl is not self:
            return self.impl.close()
        self.stop_event.set()
        self.renew_thread.join(timeout=2)
        self.browser.cancel()
        if self.info:
            self.zc.unregister_service(self.info)
        self.zc.close()
