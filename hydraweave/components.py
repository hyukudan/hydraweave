"""Component discovery and immutable source downloads on the controller.

An upstream HEAD is an available source revision, never a qualified deployment.
The existing installer owns builds and activation on SSH nodes; this module does
not edit versions.lock, presets, a working checkout or a running driver.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid

from . import compat, config, engines, redaction
from .winproc import no_window, retry_sharing


SHA = re.compile(r"[0-9a-f]{40}")
MAX_STATE_BYTES = 256 * 1024
CHECK_MAX_AGE_S = 24 * 3600
CHECK_TIMEOUT_S = 45
DOWNLOAD_TIMEOUT_S = 1800
MARKER = ".hydraweave-components.json"
SCHEMA = "hydraweave/component-cache-v1"
RECEIPT_SCHEMA = "hydraweave/component-source-v1"


@dataclass(frozen=True)
class Component:
    id: str
    label: str
    repo: str
    role: str
    activation: str


CATALOG = (
    Component("engine", "Strix Halo Engine", "https://github.com/hyukudan/strix-halo-engine-lab",
              "Model execution on inference nodes",
              "Build and activate the complete pinned profile on its inference nodes."),
    Component("tensormeld", "TensorMeld", "https://github.com/hyukudan/tensormeld",
              "Request routing and placement",
              "The profile installer and router use their pinned TensorMeld revision."),
    Component("strix-trellis", "Strix Trellis", "https://github.com/hyukudan/strix-trellis",
              "Quantized kernels linked into supported engines",
              "Build together with the engine revision and Trellis API selected by the profile."),
    Component("usb4-driver", "Strix Halo USB4 driver", "https://github.com/hyukudan/strix-halo-usb4-driver",
              "USB4 transport on Linux inference nodes",
              "Source download only. Kernel build, installation and reboot are separate node maintenance."),
)
BY_ID = {row.id: row for row in CATALOG}
_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


class ComponentError(ValueError):
    pass


class Cancelled(ComponentError):
    pass


def component(value):
    if not isinstance(value, str) or value not in BY_ID:
        raise ComponentError("unknown component")
    return BY_ID[value]


def revision(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ComponentError("commit must be a full 40-character lowercase Git SHA")
    return value


def _plain(path):
    path = Path(path)
    for item in (path, *path.parents):
        if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
            raise ComponentError("component cache must not use symlinks or junctions")
    return path


def cache_root(config_path):
    value = compat.getenv("HYDRAWEAVE_COMPONENT_CACHE")
    path = Path(value) if value is not None else Path(config_path).absolute().parent / "components"
    if not path.is_absolute() or ".." in path.parts or path == Path(path.anchor):
        raise ComponentError("HYDRAWEAVE_COMPONENT_CACHE must be an absolute, dedicated cache directory")
    return _plain(path)


def _read(path, default=None):
    _plain(path)
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_STATE_BYTES + 1)
    except FileNotFoundError:
        return deepcopy(default)
    if len(raw) > MAX_STATE_BYTES:
        raise ComponentError("component state exceeds its size limit")
    try:
        return json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise ComponentError("component state is unreadable") from exc


def _write(path, value):
    _plain(path)
    payload = (json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    if len(payload) > MAX_STATE_BYTES:
        raise ComponentError("component state exceeds its size limit")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".state-", delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        retry_sharing(lambda: os.replace(temporary, path))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def _lock(path):
    """No waiting behind another portal or CLI process doing the same operation."""
    _plain(path)
    key = os.path.normcase(str(path.absolute()))
    with _locks_guard:
        thread_lock = _locks.setdefault(key, threading.Lock())
    if not thread_lock.acquire(blocking=False):
        raise PermissionError("a component operation is already running; wait for it to finish")
    stream = None
    acquired = False
    try:
        stream = path.open("a+b")
        if os.name == "nt":
            import msvcrt
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise PermissionError("a component operation is already running in another process") from exc
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PermissionError("a component operation is already running in another process") from exc
        acquired = True
        yield
    finally:
        if stream is not None:
            if acquired:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()
        thread_lock.release()


def _stop(process):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False, **no_window())
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=3)


def git(arguments, *, cwd=None, cancelled=lambda: False, timeout=CHECK_TIMEOUT_S):
    """Bounded output and deadline, cancellable process tree; never invoke a shell."""
    if cancelled():
        raise Cancelled("component operation cancelled")
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GIT_OPTIONAL_LOCKS="0")
    argv = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "protocol.ext.allow=never",
            "-c", "protocol.file.allow=never", "-c", "http.followRedirects=false",
            "-c", "filter.lfs.required=false", "-c", "filter.lfs.smudge=", "-c", "filter.lfs.process=", *arguments]
    output = bytearray()
    overflow = threading.Event()
    with subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, start_new_session=os.name != "nt", **no_window()) as process:
        def drain():
            while True:
                block = process.stdout.read(4096)
                if not block:
                    break
                if len(output) + len(block) > MAX_STATE_BYTES:
                    overflow.set()
                    break
                output.extend(block)
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if cancelled():
                    raise Cancelled("component operation cancelled")
                if overflow.is_set():
                    raise ComponentError("Git output exceeded the component operation limit")
                if time.monotonic() >= deadline:
                    raise ComponentError("Git operation timed out; check connectivity and retry")
                time.sleep(0.05)
            reader.join(timeout=3)
            if overflow.is_set() or reader.is_alive():
                raise ComponentError("Git output exceeded the component operation limit")
            text = output.decode("utf-8", errors="replace")
            if process.returncode:
                detail = redaction.redact_text(text[-3000:].strip())
                raise ComponentError("Git failed" + (": " + detail if detail else ""))
            return text
        except BaseException:
            _stop(process)
            reader.join(timeout=3)
            raise


def catalog(cluster=None, versions=None):
    """Configured immutable pins, including per-preset overrides; no network or file writes."""
    versions = versions if versions is not None else engines.lock()
    rows = {item.id: {"id": item.id, "label": item.label, "repo": item.repo, "role": item.role, "pins": [],
                     "activation": {"automatic": False, "note": item.activation}, "why": {}}
            for item in CATALOG}

    def add(name, pin, source, profile=None, qualified=False):
        if name not in rows:
            return
        row = rows[name]
        if pin.get("repo", "").removesuffix(".git").rstrip("/") != row["repo"]:
            raise ComponentError(f"{name}: configured repository does not match the supported component")
        commit, prefix = pin.get("commit"), pin.get("commit_prefix")
        if commit is not None:
            revision(commit)
        elif not isinstance(prefix, str) or not re.fullmatch(r"[0-9a-f]{7,39}", prefix):
            raise ComponentError(f"{name}: configured component has no immutable revision")
        existing = next((p for p in row["pins"] if (p["commit"], p.get("commit_prefix")) == (commit, prefix)), None)
        if existing is None:
            existing = {"commit": commit, "commit_prefix": prefix, "profiles": [], "source": [],
                        "qualified": False, "note": pin.get("note")}
            row["pins"].append(existing)
        if source not in existing["source"]:
            existing["source"].append(source)
        if profile is not None and profile not in existing["profiles"]:
            existing["profiles"].append(profile)
        existing["qualified"] = existing["qualified"] or qualified

    for name, pin in versions["components"].items():
        add(name, pin, "versions.lock")
    profiles = []
    for name, settings in (cluster.profiles.items() if cluster is not None else ()):
        preset_name = settings.get("preset", name)
        data = config.preset(preset_name)
        selected = engines.components(data, versions)
        family = engines.family_of(data, preset_name)
        mids = engines.members(settings, family)
        for cid, pin in selected.items():
            add(cid, pin, "preset:" + preset_name if cid in data.get("components", {}) else "versions.lock",
                name, data.get("qualified") is True)
        profiles.append({"id": name, "preset": preset_name, "family": family, "machines": mids,
                         "pins": {cid: {k: p.get(k) for k in ("commit", "commit_prefix", "repo")}
                                  for cid, p in selected.items()}, "qualified": data.get("qualified") is True})
    rows["usb4-driver"]["why"]["pins"] = (
        "No USB4 kernel/driver combination is pinned by Hydraweave's profile installer; upstream sources can be staged.")
    return list(rows.values()), profiles


class Manager:
    def __init__(self, config_path, *, root=None, runner=git, clock=time.time, versions=None):
        self.config_path = Path(config_path)
        self.root = Path(root) if root is not None else cache_root(config_path)
        self.runner, self.clock, self.versions = runner, clock, versions

    def _ensure(self):
        root = _plain(self.root)
        if not root.is_absolute() or ".." in root.parts or root == Path(root.anchor):
            raise ComponentError("component cache must be an absolute, dedicated directory")
        if root.exists() and not root.is_dir():
            raise ComponentError("component cache is not a directory")
        marker = _read(root / MARKER)
        if marker is None:
            if root.exists() and any(root.iterdir()):
                raise ComponentError("component cache is nonempty and not managed by Hydraweave")
            root.mkdir(parents=True, exist_ok=True)
            _write(root / MARKER, {"schema": SCHEMA})
        elif marker != {"schema": SCHEMA}:
            raise ComponentError("component cache has an unknown marker")
        return root

    def _checks(self):
        value = _read(self.root / "checks.json", {})
        if not isinstance(value, dict) or set(value) - set(BY_ID):
            raise ComponentError("component check records are invalid")
        for item in value.values():
            if (not isinstance(item, dict) or set(item) != {"commit", "branch", "checked_at", "error"}
                    or item["commit"] is not None and (not isinstance(item["commit"], str) or not SHA.fullmatch(item["commit"]))
                    or type(item["checked_at"]) not in (int, float) or not math.isfinite(item["checked_at"])
                    or item["error"] is not None and (not isinstance(item["error"], str) or len(item["error"]) > 3000)
                    or item["branch"] is not None and (not isinstance(item["branch"], str) or len(item["branch"]) > 128)):
                raise ComponentError("component check records are invalid")
        return value

    def downloads(self, cid):
        directory = _plain(self.root / component(cid).id)
        if not directory.is_dir():
            return []
        found = []
        for path in sorted(directory.iterdir()):
            if not SHA.fullmatch(path.name):
                continue
            try:
                _plain(path)
                value = _read(path / "receipt.json")
                if (not isinstance(value, dict) or value.get("schema") != RECEIPT_SCHEMA or value.get("component") != cid
                        or value.get("commit") != path.name or value.get("repo") != component(cid).repo
                        or value.get("state") != "staged" or not (path / "checkout").is_dir()
                        or type(value.get("downloaded_at")) not in (int, float)
                        or not math.isfinite(value["downloaded_at"])):
                    continue
                _plain(path / "checkout")
                found.append({"commit": path.name, "state": "staged", "path": str(path / "checkout"),
                              "downloaded_at": value.get("downloaded_at"), "submodules_fetched": False,
                              "verification": "Commit verified when staged; rechecked before reuse"})
            except (OSError, ValueError):
                continue
        return sorted(found, key=lambda r: r.get("downloaded_at") or 0, reverse=True)[:64]

    def state(self, cluster=None, *, config_error=None, launches=None):
        rows, profiles = catalog(cluster, self.versions)
        error = None
        try:
            _plain(self.root)
            if self.root.exists():
                marker = _read(self.root / MARKER)
                if marker != {"schema": SCHEMA} and (marker is not None or any(self.root.iterdir())):
                    raise ComponentError("component cache is not a recognized managed directory")
            checks = self._checks()
        except (OSError, ValueError) as exc:
            checks, error = {}, str(exc)
        for row in rows:
            current = checks.get(row["id"])
            row["upstream"] = dict(current) if isinstance(current, dict) else None
            if row["upstream"] is not None:
                elapsed = self.clock() - row["upstream"]["checked_at"]
                row["upstream"]["stale"] = not 0 <= elapsed <= CHECK_MAX_AGE_S
                row["upstream"]["download_allowed"] = bool(row["upstream"]["commit"] and not row["upstream"]["error"]
                                                           and not row["upstream"]["stale"] and not error)
            try:
                row["downloads"] = self.downloads(row["id"])
            except (OSError, ValueError) as exc:
                row["downloads"] = []
                row["why"]["downloads"] = str(exc)
            pins = {p["commit"] for p in row["pins"] if p["commit"]}
            head = (row["upstream"] or {}).get("commit")
            row["update_available"] = head not in pins if head and pins else None
            row["upstream_is_qualified"] = any(p["commit"] == head and p["qualified"] for p in row["pins"]) if head else False
            row["download"] = {"enabled": not error, **({"why": error} if error else {})}
            row["active"] = []
            for profile, launch in (launches or {}).items():
                commit = ((launch.get("launch") or {}).get("components") or {}).get(row["id"])
                if isinstance(commit, str) and SHA.fullmatch(commit):
                    row["active"].append({"profile": profile, "commit": commit, "machines": launch.get("nodes", []),
                                          "state": launch.get("phase"), "evidence": "saved-launch",
                                          "started_at": launch.get("started_at"), "live_verified": False})
            if not row["active"]:
                row["why"]["active"] = "No saved launch manifest reports this component; live installation is unknown."
            if row["upstream"] is None:
                row["why"]["upstream"] = "Not checked yet"
        return {"components": rows, "profiles": profiles, "storage": {"scope": "controller", "path": str(self.root),
                "note": "Downloaded sources stay on the controller. Install a profile to fetch/build its pins on its SSH nodes.",
                "error": error}, "config_error": config_error,
                "checks_are_read_only": True, "activation_is_automatic": False}

    def check(self, cid=None, *, report=lambda text: None, cancelled=lambda: False):
        ids = [component(cid).id] if cid is not None else list(BY_ID)
        self._ensure()
        result = {}
        for name in ids:
            if cancelled():
                raise Cancelled("component check cancelled")
            row = component(name)
            report("Checking " + row.label + " upstream")
            checked = {"commit": None, "branch": None, "checked_at": self.clock(), "error": None}
            try:
                output = self.runner(["ls-remote", "--symref", row.repo + ".git", "HEAD"],
                                     cancelled=cancelled, timeout=CHECK_TIMEOUT_S)
                for line in output.splitlines():
                    fields = line.split("\t")
                    if len(fields) == 2 and fields[1] == "HEAD":
                        if SHA.fullmatch(fields[0]):
                            checked["commit"] = fields[0]
                        elif fields[0].startswith("ref: refs/heads/"):
                            checked["branch"] = fields[0][len("ref: refs/heads/"):][:128]
                if checked["commit"] is None:
                    raise ComponentError("upstream did not return an immutable HEAD commit")
            except Cancelled:
                raise
            except (OSError, ValueError) as exc:
                checked["error"] = redaction.redact_text(str(exc))[:3000]
            with _lock(self.root / ".checks.lock"):
                checks = self._checks()
                checks[name] = checked
                _write(self.root / "checks.json", checks)
            result[name] = checked
            report(row.label + (": check failed" if checked["error"] else ": upstream " + checked["commit"][:12]))
        return {"checked": result, "errors": sum(bool(v["error"]) for v in result.values()), "installed": False}

    def allowed(self, cid, commit, cluster=None):
        row = component(cid)
        revision(commit)
        rows, _ = catalog(cluster, self.versions)
        pins = next(r for r in rows if r["id"] == cid)["pins"]
        if any(pin["commit"] == commit for pin in pins):
            return "configured-pin"
        checked = self._checks().get(row.id) or {}
        when = checked.get("checked_at")
        if (checked.get("commit") == commit and not checked.get("error") and type(when) in (int, float)
                and 0 <= self.clock() - when <= CHECK_MAX_AGE_S):
            return "upstream-check"
        raise PermissionError("revision is neither a configured pin nor the latest successful upstream check; check for updates again")

    def _verify(self, destination, cid, commit, *, cancelled):
        _plain(destination)
        receipt = _read(destination / "receipt.json")
        expected = {"schema": RECEIPT_SCHEMA, "component": cid, "commit": commit, "repo": component(cid).repo,
                    "state": "staged"}
        if not isinstance(receipt, dict) or any(receipt.get(key) != value for key, value in expected.items()):
            raise ComponentError("existing source directory has no matching receipt; it was left untouched")
        checkout = _plain(destination / "checkout")
        _plain(checkout / ".git")
        if not checkout.is_dir() or not (checkout / ".git").is_dir():
            raise ComponentError("existing source checkout is incomplete; it was left untouched")
        head = self.runner(["rev-parse", "--verify", "HEAD"], cwd=checkout, cancelled=cancelled).strip()
        origin = self.runner(["remote", "get-url", "origin"], cwd=checkout, cancelled=cancelled).strip()
        status = self.runner(["status", "--porcelain=v1", "--untracked-files=all"], cwd=checkout, cancelled=cancelled)
        if head != commit or origin != component(cid).repo + ".git" or status.strip():
            raise ComponentError("existing source checkout is modified or has another revision; it was left untouched")
        return {**receipt, "path": str(checkout), "reused": True, "installed": False}

    def download(self, cid, commit, cluster=None, *, report=lambda text: None, cancelled=lambda: False):
        row = component(cid)
        basis = self.allowed(cid, commit, cluster)
        self._ensure()
        directory = _plain(self.root / row.id)
        directory.mkdir(exist_ok=True)
        with _lock(directory / ".download.lock"):
            # Repeat the check after acquiring the lock: another check may have superseded the requested HEAD.
            basis = self.allowed(cid, commit, cluster)
            destination = _plain(directory / commit)
            if destination.exists():
                report("Verifying the already downloaded " + row.label + " source")
                return self._verify(destination, cid, commit, cancelled=cancelled)
            stage = directory / (".download-" + uuid.uuid4().hex)
            checkout = stage / "checkout"
            deadline = time.monotonic() + DOWNLOAD_TIMEOUT_S

            def run(arguments):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ComponentError("source download timed out; retry to download this revision")
                return self.runner(arguments, cwd=checkout, cancelled=cancelled, timeout=remaining)

            try:
                checkout.mkdir(parents=True)
                report("Preparing " + row.label + " source " + commit[:12])
                run(["init", "--quiet", "--template="])
                run(["remote", "add", "origin", row.repo + ".git"])
                report("Downloading " + row.label + " " + commit[:12])
                run(["fetch", "--quiet", "--no-tags", "--depth=1", "origin", commit])
                actual = run(["rev-parse", "--verify", "FETCH_HEAD^{commit}"]).strip()
                if actual != commit:
                    raise ComponentError("downloaded revision does not match the requested commit")
                report("Verifying and staging " + row.label + " source")
                run(["checkout", "--quiet", "--detach", commit, "--"])
                if run(["status", "--porcelain=v1", "--untracked-files=all"]).strip():
                    raise ComponentError("new source checkout is not clean")
                if cancelled():
                    raise Cancelled("source download cancelled before publication")
                receipt = {"schema": RECEIPT_SCHEMA, "component": cid, "commit": commit, "repo": row.repo,
                           "state": "staged", "downloaded_at": self.clock(), "selection": basis,
                           "submodules_fetched": False}
                _write(stage / "receipt.json", receipt)
                retry_sharing(lambda: os.rename(stage, destination))
                report(row.label + " source downloaded; the active deployment is unchanged")
                return {**receipt, "path": str(destination / "checkout"), "reused": False, "installed": False}
            finally:
                if stage.exists() and not stage.is_symlink():
                    shutil.rmtree(stage)


def install_plan(cluster, profile):
    """Explain the existing installer; the plan itself never contacts a node."""
    if not isinstance(profile, str) or profile not in cluster.profiles:
        raise LookupError("unknown profile")
    settings = cluster.profiles[profile]
    preset_name = settings.get("preset", profile)
    data = config.preset(preset_name)
    family = engines.family_of(data, preset_name)
    mids = engines.members(settings, family)
    pins = engines.components(data)
    blockers = []
    if any(cluster.machines[mid].access != "ssh" for mid in mids):
        blockers.append("The current profile installer requires SSH access to each inference node.")
    if any(not row.get("commit") for row in pins.values()):
        blockers.append("This preset contains unpublished component pins; supply the matching local source with the CLI installer.")
    return {"profile": profile, "preset": preset_name, "machines": mids, "build_on": mids[0] if mids else None,
            "components": [{"id": name, **pin} for name, pin in pins.items()],
            "action": {"endpoint": "install", "arguments": {"profile": profile}, "enabled": not blockers},
            "blockers": blockers, "qualified": data.get("qualified") is True,
            "steps": [
                {"id": "node-tool", "label": "Update and verify the node tools", "where": mids,
                 "action": "ops/nodes-update", "note": "Connect over SSH with the pinned host keys."},
                {"id": "fetch", "label": "Download the profile's exact component revisions", "where": mids[:1],
                 "note": "Fetched directly by the build node; controller source downloads do not activate a release."},
                {"id": "build", "label": "Build and verify one complete engine release", "where": mids[:1],
                 "note": "Uses the profile's compiler, Trellis API and required capabilities."},
                {"id": "distribute", "label": "Distribute and verify the release", "where": mids[1:],
                 "note": "Peers receive the same built binaries and hashes."},
                {"id": "activate", "label": "Activate the release for the stopped profile", "where": mids,
                 "note": "A running profile blocks activation; prior release references are retained for rollback."}],
            "driver": {"automatic": False, "note": "Kernel/USB4 driver installation is separate node maintenance."},
            "starts_inference": False}
