"""Owned, bounded model I/O jobs and direct SSH transfers (stdlib, Python 3.10+).

The controller only carries a bounded manifest and progress. Model bytes travel
directly from source to receiver. A private, resumable staging tree is verified
before publication; final files are never overwritten. Both nodes hold their
model-I/O gate, mutually exclusive with admission of a new engine lease.
"""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import hashlib
import ipaddress
import json
import ctypes
import os
from pathlib import Path
import re
import selectors
import select
import shlex
import subprocess
import sys
import time
import uuid

import hydraweave_install as install
import hydraweave_models as models

MAX_FRAME = 4 * 1024 * 1024
VERBS = set()  # shipped as a helper; hydraweave_models owns the public verbs
OWNER_TTL = 120
MAX_DURATION = 24 * 3600
LIVE = {"queued", "running"}


def _lock_path(root):
    return Path(root) / "run" / "model-io.lock"


def _lock(root):
    if models.fcntl is None:
        raise ValueError("managed model I/O jobs require POSIX file locks")
    fd = os.open(_lock_path(root), os.O_CREAT | os.O_RDWR | models.NOFOLLOW, 0o600)
    try:
        models.fcntl.flock(fd, models.fcntl.LOCK_EX | models.fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise ValueError("a model transfer or verification owns this node") from None
    return fd


def refuse_storage_owner(root):
    """Called under the node's root_lock before a new engine lease is recorded."""
    fd = _lock(root)
    os.close(fd)


def quiet(policy):
    from hydraweave_download import lab_quiet, no_lease
    lab_quiet(policy.lab)
    no_lease(policy.root)


@contextmanager
def storage_owner(policy):
    from hydraweave_lease import root_lock
    fd = None
    with root_lock(policy.root):
        from hydraweave_agent_update import require_idle
        require_idle(policy.root)
        fd = _lock(policy.root)
        try:
            quiet(policy)
        except BaseException:
            os.close(fd)
            raise
    try:
        yield
    finally:
        os.close(fd)


def _unit(tree, name, check=None):
    """A complete manifest, plus the exact small manifest files that describe it."""
    left = models.request_left()
    budget = models.Budget(time.monotonic() + (30 if left is None else max(.001, min(30, left))), check)
    units, siblings, _, hit = models.root_listing(tree, budget)
    unit = next((u for u in units if u[0] == name), None)
    if hit or unit is None:
        raise ValueError("model unit missing or inventory incomplete: " + str(hit or name))
    if unit[1] == "dir":
        files, manifests, links, hit = models.walk_unit(tree, name, budget)
    else:
        files, manifests, links = {name: (unit[2], unit[3])}, [], 0
    if links or hit:
        raise ValueError("copy requires a bounded unit without symlinks")
    sibling_rows = {s: models.parse_manifest(tree, s, size, budget) for s, size in siblings.items()}
    index = models.sibling_index(sibling_rows, budget)
    entries, invalid, repeated = models.unit_entries(tree, name, files, manifests, index, budget)
    data = set(files) - set(manifests)
    if invalid or repeated or not data or set(entries) != data or any(len(v) != 1 for v in entries.values()):
        raise ValueError("copy requires one complete, unambiguous SHA-256 manifest for every file")
    rows = []
    for path in sorted(data):
        reader = tree.open(path, files[path][0])
        try:
            identity = list(reader.identity)
        finally:
            reader.close()
        rows.append({"path": path, "bytes": files[path][0], "sha256": next(iter(entries[path])),
                     "identity": identity})
    # A normalized manifest is generated at the receiver, so outside sibling
    # manifests and unrelated model units never need to cross the link.
    return {"name": name, "kind": unit[1], "files": rows, "bytes": sum(r["bytes"] for r in rows)}


def plan(payload):
    policy = models.effective_policy(payload)
    name = models.unit_name(payload.get("name"))
    with models.Tree(payload.get("model_root")) as tree:
        document = _unit(tree, name)
        document["model_root"] = str(tree.root)
    peer = payload.get("peer")
    _peer(peer)
    document["peer"] = peer
    document["policy"] = policy.payload()
    document["confirm"] = hashlib.sha256(install.canonical(document)).hexdigest()[:16]
    return document


def _peer(peer):
    if not isinstance(peer, dict) or set(peer) != {"target", "address", "port", "host_key", "tool", "policy", "model_root"}:
        raise ValueError("copy requires a configured, pinned SSH peer")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,199}", str(peer["target"])):
        raise ValueError("invalid peer SSH target")
    ipaddress.ip_address(peer["address"])
    if type(peer["port"]) is not int or not 1 <= peer["port"] <= 65535:
        raise ValueError("invalid peer SSH port")
    key = peer["host_key"]
    if not isinstance(key, str) or not re.fullmatch(r"(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) [A-Za-z0-9+/=]+", key):
        raise ValueError("peer host key must be explicitly pinned")
    install.absolute(peer["tool"])
    install.absolute(peer["model_root"])
    if not isinstance(peer["policy"], dict):
        raise ValueError("peer policy required")


class Wire:
    """Nonblocking framing; cancellation/deadlines remain live under pipe stalls."""
    def __init__(self, input_fd, output_fd, check):
        self.input, self.output, self.check = input_fd, output_fd, check
        self.buffer = bytearray()
        os.set_blocking(input_fd, False)
        os.set_blocking(output_fd, False)

    def _wait(self, fd, event):
        while True:
            self.check()
            readable, writable, _ = select.select([fd] if event == selectors.EVENT_READ else [],
                                                  [fd] if event == selectors.EVENT_WRITE else [], [], .25)
            if readable or writable:
                return

    def send(self, data):
        view = memoryview(data)
        while view:
            self.check()
            try:
                count = os.write(self.output, view)
            except BlockingIOError:
                self._wait(self.output, selectors.EVENT_WRITE)
                continue
            if count <= 0:
                raise ValueError("peer closed the transfer")
            view = view[count:]

    def read(self, maximum):
        if self.buffer:
            result = bytes(self.buffer[:maximum])
            del self.buffer[:maximum]
            return result
        while True:
            self.check()
            try:
                data = os.read(self.input, min(maximum, 1024 * 1024))
            except BlockingIOError:
                self._wait(self.input, selectors.EVENT_READ)
                continue
            if not data:
                raise ValueError("peer closed the transfer before completion")
            return data

    def json(self, value):
        data = install.canonical(value) + b"\n"
        if len(data) > MAX_FRAME:
            raise ValueError("transfer manifest exceeds the size limit")
        self.send(data)

    def receive(self):
        while b"\n" not in self.buffer:
            chunk = self.read(65536) if not self.buffer else self._more()
            self.buffer.extend(chunk)
            if len(self.buffer) > MAX_FRAME:
                raise ValueError("peer reply exceeds the size limit")
        line, _, rest = self.buffer.partition(b"\n")
        self.buffer[:] = rest
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("peer reply is not an object")
        if value.get("error"):
            raise ValueError("peer: " + str(value["error"])[:1024])
        return value

    def _more(self):
        existing, self.buffer = self.buffer, bytearray()
        try:
            return self.read(65536)
        finally:
            self.buffer = existing


def _validated_rows(document):
    name = models.unit_name(document.get("name"))
    if document.get("kind") not in ("dir", "file"):
        raise ValueError("invalid model unit kind")
    rows = document.get("files")
    if not isinstance(rows, list) or not 1 <= len(rows) <= models.MAX_ENTRIES:
        raise ValueError("invalid transfer file count")
    seen = set()
    for row in rows:
        path = models.rel_path(row.get("path"))
        if path in seen or (document["kind"] == "dir" and not path.startswith(name + "/")) or \
                (document["kind"] == "file" and path != name):
            raise ValueError("transfer file lies outside the selected unit")
        seen.add(path)
        if type(row.get("bytes")) is not int or row["bytes"] < 0 or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("sha256"))):
            raise ValueError("every transfer file needs size and SHA-256")
    return rows


def _digest(tree, row, check, progress=None):
    with_reader = tree.open(row["path"], row["bytes"])
    try:
        return models.hash_reader(with_reader, check, progress)
    finally:
        with_reader.close()


def _ssh_path(path):
    value = str(path).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    if any(ord(c) < 32 for c in value):
        raise ValueError("invalid SSH known-hosts path")
    return '"' + value + '"' if any(c.isspace() for c in value) else value


def _rename_new(source_fd, source, destination_fd, destination):
    """Linux rename without replacing even an empty, concurrently created dir."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise ValueError("atomic no-replace directory publication requires Linux renameat2")
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(source_fd, os.fsencode(source), destination_fd, os.fsencode(destination), 1):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def _publish_file(staging, final, name, manifest_name):
    """Publish a file and sidecar, rolling back only this attempt's own link."""
    expected = os.stat(name, dir_fd=staging.fd, follow_symlinks=False)
    try:
        os.stat(manifest_name, dir_fd=final.fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise ValueError("destination manifest already exists; nothing overwritten")
    os.link(name, name, src_dir_fd=staging.fd, dst_dir_fd=final.fd, follow_symlinks=False)
    try:
        os.link(manifest_name, manifest_name, src_dir_fd=staging.fd, dst_dir_fd=final.fd, follow_symlinks=False)
    except BaseException:
        try:
            current = os.stat(name, dir_fd=final.fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino):
                os.unlink(name, dir_fd=final.fd)
        except FileNotFoundError:
            pass
        raise
    os.unlink(name, dir_fd=staging.fd)
    os.unlink(manifest_name, dir_fd=staging.fd)


class WriteTree(models.Tree):
    """A writer root pinned without following any absolute-path component."""
    def __init__(self, root, fd=None):
        self.root = Path(root)
        self.fd = None
        if fd is not None:
            self.fd = os.dup(fd)
            return
        models.read_root(str(root))
        anchor = os.open(self.root.anchor, os.O_RDONLY | models.DIRECTORY | models.NOFOLLOW)
        try:
            self.fd = models.open_dir_chain(anchor, self.root.parts[1:])
        finally:
            os.close(anchor)


def _exists(tree, name):
    try:
        os.stat(name, dir_fd=tree.fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def receive(wire, first=None):
    document = first or wire.receive()
    rows = _validated_rows(document)
    policy = models.effective_policy(document["policy"])
    root = models.read_root(document["model_root"])
    if root.is_symlink() or root.resolve() != root:
        raise ValueError("destination root must have no symlink ancestors")
    name = document["name"]
    expected = {r["path"]: r["sha256"] for r in rows}
    stage_id = hashlib.sha256(install.canonical({"name": name, "files": expected})).hexdigest()[:24]
    stage = root / (".hydraweave-incoming-" + stage_id)
    transferred, reused = 0, 0
    original_check = wire.check
    next_policy_check = [0.0]
    next_progress = [0.0]

    def check():
        original_check()
        if time.monotonic() >= next_policy_check[0]:
            next_policy_check[0] = time.monotonic() + .25
            quiet(policy)

    wire.check = check

    def hashing(row):
        def report(count):
            if time.monotonic() >= next_progress[0]:
                next_progress[0] = time.monotonic() + .5
                wire.json({"progress": {"phase": "verifying resident content", "file": row["path"], "bytes_hashed": count}})
        return report

    with storage_owner(policy), WriteTree(root) as final:
        # Existing content is reusable only after an exact complete hash check.
        if _exists(final, name):
            existing = _unit(final, name, check)
            if {r["path"]: r["sha256"] for r in existing["files"]} != expected:
                raise ValueError("destination unit exists with different contents; nothing overwritten")
            for row in rows:
                if _digest(final, row, check, hashing(row)) != row["sha256"]:
                    raise ValueError("existing destination failed integrity verification")
            result = {"state": "succeeded", "bytes_transferred": 0,
                      "bytes_reused": sum(r["bytes"] for r in rows), "files": len(rows), "destination": str(root / name)}
            wire.json({"complete": result})
            return result
        if document["kind"] == "file":
            try:
                os.stat(name + ".sha256", dir_fd=final.fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ValueError("destination manifest already exists; no model bytes requested")
        try:
            os.mkdir(stage.name, mode=0o700, dir_fd=final.fd)
        except FileExistsError:
            pass
        stage_fd = os.open(stage.name, os.O_RDONLY | models.DIRECTORY | models.NOFOLLOW, dir_fd=final.fd)
        staging = WriteTree(stage, stage_fd)
        os.close(stage_fd)
        with staging:
            have = set()
            for row in rows:
                try:
                    if _digest(staging, row, check, hashing(row)) == row["sha256"]:
                        have.add(row["path"])
                except (FileNotFoundError, ValueError):
                    pass
            required = sum(r["bytes"] for r in rows if r["path"] not in have)
            disk = os.fstatvfs(staging.fd)
            if disk.f_bavail * disk.f_frsize < required + 1024 * 1024:
                raise ValueError("insufficient destination space for missing files and manifest")
            for row in rows:
                check()
                path = row["path"]
                if path in have:
                    reused += row["bytes"]
                    wire.json({"path": path, "need": False})
                    continue
                # Open below a pinned dirfd; neither parent swaps nor links can
                # redirect model data to files outside this staging directory.
                parts = path.split("/")
                directory = models.open_dir_chain(staging.fd, parts[:-1], create=True)
                temporary = ".partial-" + uuid.uuid4().hex
                fd = None
                try:
                    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | models.NOFOLLOW, 0o600, dir_fd=directory)
                    wire.json({"path": path, "need": True})
                    remaining, digest = row["bytes"], hashlib.sha256()
                    while remaining:
                        check()
                        chunk = wire.read(min(1024 * 1024, remaining))
                        digest.update(chunk)
                        view = memoryview(chunk)
                        while view:
                            count = os.write(fd, view)
                            view = view[count:]
                        remaining -= len(chunk)
                        transferred += len(chunk)
                    os.fsync(fd)
                    if hasattr(os, "posix_fadvise"):
                        models.drop_cache(fd)
                    os.close(fd)
                    fd = None
                    if digest.hexdigest() != row["sha256"]:
                        raise ValueError("transferred file failed SHA-256 verification")
                    check()
                    os.replace(temporary, parts[-1], src_dir_fd=directory, dst_dir_fd=directory)
                    os.fsync(directory)
                    wire.json({"verified": path})
                finally:
                    if fd is not None:
                        os.close(fd)
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except FileNotFoundError:
                        pass
                    os.close(directory)
            check()
            if wire.receive() != {"commit": True}:
                raise ValueError("source did not commit the verified transfer")
            manifest_name = name + ".sha256" if document["kind"] == "file" else name + "/hydraweave-copy.sha256"
            manifest = "".join(r["sha256"] + "  " + (r["path"] if document["kind"] == "file" else r["path"][len(name) + 1:]) + "\n" for r in rows)
            parts = manifest_name.split("/")
            directory = models.open_dir_chain(staging.fd, parts[:-1])
            temporary = ".manifest-" + uuid.uuid4().hex
            try:
                fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | models.NOFOLLOW, 0o600, dir_fd=directory)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(manifest.encode("utf-8"))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, parts[-1], src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            finally:
                os.close(directory)
            check()
            if _exists(final, name):
                raise ValueError("destination appeared during the transfer")
            # Lifetime per-node lock excludes our other copies; rename cannot
            # replace a populated directory. File publication uses link O_EXCL.
            if document["kind"] == "dir":
                _rename_new(staging.fd, name, final.fd, name)
            else:
                _publish_file(staging, final, name, manifest_name)
            os.fsync(final.fd)
        try:
            os.rmdir(stage.name, dir_fd=final.fd)
        except OSError:
            pass
        result = {"state": "succeeded", "bytes_transferred": transferred, "bytes_reused": reused,
                  "files": len(rows), "destination": str(root / name)}
        wire.json({"complete": result})
        return result


def transfer(document, run_dir, check, progress=lambda value: None, command=None):
    rows = _validated_rows(document)
    peer = document["peer"]
    key_file = Path(run_dir) / ("copy-known-hosts-" + uuid.uuid4().hex)
    key_file.write_text("hydraweave-copy-peer " + peer["host_key"] + "\n", encoding="ascii")
    key_file.chmod(0o600)
    if command is None:
        command = ["ssh", "-F", "/dev/null", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                   "-o", "StrictHostKeyChecking=yes", "-o", "GlobalKnownHostsFile=none", "-o", "UpdateHostKeys=no",
                   "-o", "UserKnownHostsFile=" + _ssh_path(key_file), "-o", "HostKeyAlias=hydraweave-copy-peer",
                   "-o", "Hostname=" + peer["address"], "-p", str(peer["port"]), "-T", "--", peer["target"],
                   "python3 " + shlex.quote(peer["tool"]) + " --receive"]
    process = None
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        wire = Wire(process.stdout.fileno(), process.stdin.fileno(), check)
        wire.json({"name": document["name"], "kind": document["kind"], "files": rows,
                   "policy": peer["policy"], "model_root": peer["model_root"]})
        transferred = 0

        def completed(result):
            if not isinstance(result, dict) or result.get("state") != "succeeded":
                raise ValueError("peer did not confirm atomic publication")
            if (type(result.get("files")) is not int or result["files"] != len(rows)
                    or result.get("destination") != str(Path(peer["model_root"]) / document["name"])
                    or any(type(result.get(k)) is not int or result[k] < 0 for k in ("bytes_transferred", "bytes_reused"))
                    or result["bytes_transferred"] + result["bytes_reused"] != sum(r["bytes"] for r in rows)):
                raise ValueError("peer completion does not match the planned file identity and byte counts")
            return result

        def response():
            while True:
                value = wire.receive()
                if "progress" not in value:
                    return value
                progress(value["progress"])

        with models.Tree(document["model_root"]) as tree:
            reply = response()
            if "complete" in reply:
                return completed(reply["complete"])
            for index, row in enumerate(rows):
                if index:
                    reply = response()
                if reply.get("path") != row["path"] or type(reply.get("need")) is not bool:
                    raise ValueError("peer requested an unplanned file")
                if not reply["need"]:
                    continue
                reader = tree.open(row["path"], row["bytes"], row["identity"][:2])
                try:
                    if list(reader.identity) != row["identity"]:
                        raise ValueError("source changed since the copy was planned")
                    digest, offset = hashlib.sha256(), 0
                    # One bounded read-ahead overlaps the next disk block with
                    # hashing/link writes. At most two 8 MiB payloads are live.
                    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="model-prefetch") as pool:
                        pending = pool.submit(reader.read_at, 0) if reader.size else None
                        while offset < reader.size:
                            check()
                            try:
                                chunk = pending.result(timeout=.25)
                            except FutureTimeout:
                                continue
                            chunk = chunk[:reader.size - offset]
                            if not chunk:
                                raise ValueError("source ended before the planned length")
                            next_offset = offset + len(chunk)
                            pending = pool.submit(reader.read_at, next_offset) if next_offset < reader.size else None
                            digest.update(chunk)
                            wire.send(chunk)
                            offset = next_offset
                            transferred += len(chunk)
                            progress({"bytes_transferred": transferred, "file": row["path"]})
                    if reader.current_identity() != tuple(row["identity"]) or digest.hexdigest() != row["sha256"]:
                        raise ValueError("source failed its planned identity or SHA-256")
                finally:
                    reader.close()
                if response().get("verified") != row["path"]:
                    raise ValueError("peer did not verify the transferred file")
            check()
            wire.json({"commit": True})
            result = response().get("complete")
            return completed(result)
    finally:
        if process is not None:
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()
            if process.poll() is None:
                from hydraweave_download import _stop_download
                _stop_download(process)
        key_file.unlink(missing_ok=True)


def _paths(root, job):
    job = install.identifier(job)
    return {kind: install.inside(root, "run", "job-" + job + suffix)
            for kind, suffix in (("state", ".json"), ("spec", ".spec.json"), ("owner", ".owner"), ("stop", ".stop"))}


def start(kind, payload):
    from hydraweave_lease import root_lock
    from hydraweave_agent_update import require_idle
    policy = models.effective_policy(payload)
    # Malformed requests stay read-only: reject their selected path/name before
    # provisioning ownership lock files or a queued job record.
    models.unit_name(payload.get("name"))
    models.read_root(payload.get("model_root"))
    if kind == "copy":
        _peer(payload.get("peer"))
        if not re.fullmatch(r"[0-9a-f]{16}", str(payload.get("confirm"))):
            raise ValueError("review and confirm a copy plan first")
    with root_lock(policy.root):
        require_idle(policy.root)
        refuse_storage_owner(policy.root)
        return _start(kind, payload, policy)


def _start(kind, payload, policy):
    models.unit_name(payload.get("name"))
    models.read_root(payload.get("model_root"))
    if kind == "copy":
        document = plan(payload)
        if payload.get("confirm") != document["confirm"]:
            raise ValueError("copy plan changed; review and confirm the current plan")
    else:
        document = None
    quiet(policy)
    job = uuid.uuid4().hex
    paths = _paths(policy.root, job)
    spec = {"kind": kind, "job": job, "payload": payload, "plan": document, "started": time.time()}
    install.atomic_json(paths["spec"], spec)
    paths["owner"].touch()
    log = policy.root / "log" / ("job-" + job + ".log")
    state = {"job": job, "kind": kind, "state": "queued", "created": time.time(),
             "boot_id": install.boot_id(), "log": str(log)}
    install.atomic_json(paths["state"], state)
    try:
        with log.open("ab") as output:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--job", str(paths["spec"])],
                                     stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)
        # Parent still holds root_lock. The worker writes running only after
        # acquiring storage_owner under that same lock, so this cannot clobber it.
        state["pid"] = child.pid
        install.atomic_json(paths["state"], state)
    except BaseException as exc:
        state.update(state="failed", error=str(exc), finished=time.time())
        install.atomic_json(paths["state"], state)
        paths["spec"].unlink(missing_ok=True)
        paths["owner"].unlink(missing_ok=True)
        raise
    return {"job": job, "pid": child.pid, "log": str(log)}


def job_main(spec_path):
    spec = install.read_json(install.absolute(spec_path))
    payload, job = spec["payload"], spec["job"]
    policy = models.effective_policy(payload)
    paths = _paths(policy.root, job)
    state = {"job": job, "kind": spec["kind"], "state": "running", "started": time.time(), "pid": os.getpid(),
             "boot_id": install.boot_id(), "progress": {}, "log": str(policy.root / "log" / ("job-" + job + ".log"))}
    last = [0.0]
    next_check = [0.0]

    def check():
        if time.monotonic() < next_check[0]:
            return
        next_check[0] = time.monotonic() + .25
        now = time.time()
        if paths["stop"].exists():
            raise models.Stopped("cancelled")
        if now - spec["started"] >= MAX_DURATION:
            raise models.Stopped("deadline reached")
        if not paths["owner"].exists() or now - paths["owner"].stat().st_mtime > OWNER_TTL:
            raise models.Stopped("controller stopped renewing this job")
        quiet(policy)

    def progress(value):
        state["progress"] = value
        if time.monotonic() - last[0] >= 0.5:
            last[0] = time.monotonic()
            install.atomic_json(paths["state"], state)

    try:
        with storage_owner(policy):
            install.atomic_json(paths["state"], state)
            check()
            if spec["kind"] == "verify":
                result = models.verify_unit(payload["model_root"], payload["name"], check=check,
                                            progress=lambda n: progress({"bytes_hashed": n}))
            else:
                result = transfer(spec["plan"], policy.root / "run", check, progress)
            check()
            state.update(state="succeeded", result=result)
    except models.Stopped as exc:
        state.update(state="cancelled", error=str(exc))
    except Exception as exc:
        state.update(state="failed", error=str(exc)[:2048])
    finally:
        state["finished"] = time.time()
        install.atomic_json(paths["state"], state)
        for kind in ("spec", "stop", "owner"):
            paths[kind].unlink(missing_ok=True)
    return 0 if state["state"] == "succeeded" else 1


def run(verb, payload):
    policy = models.effective_policy(payload)
    if verb == "copy-plan":
        return plan(payload)
    if verb in ("copy", "model-verify"):
        return start("copy" if verb == "copy" else "verify", payload)
    paths = _paths(policy.root, payload.get("job"))
    state = install.read_json(paths["state"])
    if state.get("kind") not in ("copy", "verify"):
        raise ValueError("not a library job")
    state = install.settle_stale(paths["state"], state)
    if state.get("state") == "queued" and time.time() - state.get("created", time.time()) > OWNER_TTL:
        state.update(state="failed", error="job never published its running state before its owner deadline", finished=time.time())
        install.atomic_json(paths["state"], state)
    if state.get("state") in LIVE:
        if verb == "copy-cancel":
            paths["stop"].touch()
        elif payload.get("renew") is True:
            paths["owner"].touch()
    return state


if __name__ == "__main__":
    if os.name == "posix":
        os.nice(19)
    if len(sys.argv) == 3 and sys.argv[1] == "--job":
        raise SystemExit(job_main(sys.argv[2]))
    if sys.argv[1:] == ["--receive"]:
        end = time.monotonic() + MAX_DURATION
        def guard():
            if time.monotonic() >= end:
                raise ValueError("transfer deadline reached")
            import select
            poller = select.poll()
            poller.register(sys.stdin.fileno(), select.POLLHUP | select.POLLERR)
            if any(flags & (select.POLLHUP | select.POLLERR) for _, flags in poller.poll(0)):
                raise ValueError("source disconnected; staging retained")
        connection = Wire(sys.stdin.fileno(), sys.stdout.fileno(), guard)
        try:
            receive(connection)
        except Exception as exc:
            try:
                connection.json({"error": str(exc)[:2048]})
            except (OSError, ValueError):
                pass
            raise SystemExit(1)
    else:
        raise SystemExit("internal job entry point only")
