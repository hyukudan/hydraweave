#!/bin/sh
# Install a verified release wheel into a private, versioned Python environment.
set -eu

case "$(uname -s)" in
    Linux|Darwin) ;;
    *) printf '%s\n' 'Unsupported OS; use windows/install.ps1 on Windows.' >&2; exit 1 ;;
esac
if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'Python 3.11+ is required (3.10+ for --node). On Debian/Ubuntu, run: sudo apt-get install python3 python3-venv' >&2
    exit 1
fi

# Python is already required for venv. Its stdlib handles JSON, HTTPS, hashing
# and quoted shim paths consistently on Linux and macOS, without jq or sudo.
exec python3 - "$@" <<'PY'
"""Verified, side-by-side releases. Stdlib only; embedded verbatim in install.sh.

Regenerate the standalone installer with tools/embed_installer.py after edits.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile


class NoPublishedRelease(ValueError):
    pass


def fail(message: str):
    raise ValueError(message)


def version(value: str) -> str:
    value = value.removeprefix("v")
    if not re.fullmatch(r"[0-9][A-Za-z0-9.!+_-]*", value):
        fail("Invalid release version")
    return value


def wheel_version(name: str) -> str:
    match = re.fullmatch(r"hydraweave-([^-]+)-py3-none-any\.whl", name)
    if not match:
        fail("Expected a hydraweave-<version>-py3-none-any.whl wheel")
    return version(match[1])


def read_url(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "hydraweave-installer",
                                                 "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def checksums(data: bytes) -> dict:
    entries = {}
    for line in data.decode("utf-8").splitlines():
        match = re.fullmatch(r"([a-fA-F0-9]{64}) [ *]([^/\\]+)", line)
        if not match or match[2] in entries:
            fail("Malformed or duplicate SHA256SUMS entry")
        entries[match[2]] = match[1].lower()
    return entries


def verify(wheel: Path, entries: dict) -> str:
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    if entries.get(wheel.name) != digest:
        fail("SHA256 mismatch or missing checksum: " + wheel.name)
    return digest


def release(requested: str | None = None) -> tuple:
    api = os.environ.get("HYDRAWEAVE_RELEASE_API",
                         "https://api.github.com/repos/hyukudan/hydraweave/releases").rstrip("/")
    endpoint = "tags/" + urllib.parse.quote("v" + version(requested), safe="") if requested else "latest"
    try:
        metadata = json.loads(read_url(api + "/" + endpoint))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise NoPublishedRelease("No published release: No matching GitHub release exists yet; "
                                     "use a source checkout until a v* tag is published.") from None
        raise
    selected = version(metadata["tag_name"])
    if requested and selected != version(requested):
        fail("Release metadata does not match --version")
    assets = {asset["name"]: asset["browser_download_url"] for asset in metadata["assets"]}
    wheel = "hydraweave-" + selected + "-py3-none-any.whl"
    if wheel not in assets or "SHA256SUMS" not in assets:
        fail("Release must contain the wheel and SHA256SUMS")
    return selected, wheel, assets


def check_python(node: bool = False):
    floor = (3, 10) if node else (3, 11)
    if sys.version_info < floor:
        fail("Python %d.%d or newer is required%s" % (floor + (" for --node" if node else ";"
             " node-only installs (--node) accept 3.10",)))
    if any(importlib.util.find_spec(module) is None for module in ("venv", "ensurepip")):
        fail("Python venv support is missing. On Debian/Ubuntu, run exactly: "
             "sudo apt-get install python3-venv")


def default_prefix() -> Path:
    if os.environ.get("HYDRAWEAVE_PREFIX"):
        return Path(os.environ["HYDRAWEAVE_PREFIX"]).expanduser().absolute()
    # sys.prefix retains the venv path even when invoked through current.
    environment = Path(sys.prefix).absolute()
    if environment.name == "current" and environment.parent.name == "hydraweave":
        return environment.parent.parent.parent
    if environment.parent.name == "venvs" and environment.parent.parent.name == "hydraweave":
        return environment.parent.parent.parent.parent
    return Path.home() / ".local"


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.!+_-]{0,159}", value) or value in {".", ".."}:
        fail("Invalid release id")
    return value


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def operation_lock(root: Path):
    """OS-owned lock: a crashed updater cannot leave a permanent lock behind."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".release-lock").open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.write(b"\0")
            stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ValueError("Another release operation is running") from None
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ValueError("Another release operation is running") from None
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


class ReleaseManager:
    def __init__(self, prefix: Path | None = None, *, runner=None, keep: int = 3):
        self.prefix = Path(prefix if prefix is not None else default_prefix()).expanduser().absolute()
        self.root = self.prefix / "share" / "hydraweave"
        self.runner = runner or subprocess.run
        self.keep = max(2, keep)

    def environment(self, rid: str) -> Path:
        return self.root / "venvs" / identifier(rid)

    def executable(self, rid: str, command: str) -> Path:
        return self.environment(rid) / ("Scripts" if os.name == "nt" else "bin") / (
            command + ".exe" if os.name == "nt" else command)

    @property
    def current(self) -> str | None:
        link = self.root / "current"
        if not link.is_symlink():
            if link.exists():
                fail("current must be a symlink")
            return None
        target = Path(os.readlink(link))
        rid = identifier(target.name)
        if (link.parent / target).absolute() != self.environment(rid):
            fail("current points outside the release environments")
        return rid

    @property
    def previous(self) -> str | None:
        try:
            value = (self.root / "previous").read_text(encoding="utf-8").strip()
            return identifier(value) if value else None
        except FileNotFoundError:
            return None

    def list(self) -> list[dict]:
        current, previous = self.current, self.previous
        rows = []
        for path in sorted((self.root / "releases").glob("*/release.json")):
            row = json.loads(path.read_text(encoding="utf-8"))
            if row["id"] != path.parent.name:
                fail("Release metadata id does not match its directory")
            rows.append(dict(row, current=row["id"] == current, previous=row["id"] == previous))
        return sorted(rows, key=lambda row: (row["created"], row["id"]), reverse=True)

    def activate(self, rid: str):
        identifier(rid)
        if not (self.root / "releases" / rid / "release.json").is_file() or not self.executable(rid, "python").is_file():
            fail("Release is not installed: " + rid)
        old = self.current
        if old == rid:
            return
        temporary = self.root / (".current-" + uuid.uuid4().hex)
        saved_previous = self.previous
        try:
            temporary.symlink_to(Path("venvs") / rid, target_is_directory=True)
            atomic_write(self.root / "previous", old or "")
            try:
                os.replace(temporary, self.root / "current")
            except BaseException:
                atomic_write(self.root / "previous", saved_previous or "")
                raise
        finally:
            temporary.unlink(missing_ok=True)

    def remove(self, rid: str):
        identifier(rid)
        if rid == self.current:
            fail("Refusing to remove the current release; activate another first")
        for path in (self.root / "releases" / rid, self.environment(rid)):
            if path.is_symlink():
                fail("Release directories must not be symlinks")
        for path in (self.root / "releases" / rid, self.environment(rid)):
            if path.exists():
                shutil.rmtree(path)
        if rid == self.previous:
            atomic_write(self.root / "previous", "")

    def prune(self):
        rows = self.list()
        protected = {self.current, self.previous} - {None}
        for row in rows:
            if len(protected) < self.keep:
                protected.add(row["id"])
        for row in rows:
            if row["id"] not in protected:
                self.remove(row["id"])

    def run(self, argv: list[str], *, timeout: int = 1800):
        result = self.runner(argv, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            fail("Command failed: " + " ".join(map(str, argv)) + ": " +
                 (result.stderr or result.stdout or "nonzero exit status").strip()[-2000:])
        return result

    def stage(self, wheel: Path, extras=(), *, offline: bool = False, commit: str | None = None) -> str:
        wheel = Path(wheel).absolute()
        selected = wheel_version(wheel.name)
        entries = checksums(wheel.with_name("SHA256SUMS").read_bytes())
        digest = verify(wheel, entries)
        if offline:
            for dependency in wheel.parent.glob("*.whl"):
                verify(dependency, entries)
        if isinstance(extras, str):
            extras = [extras]
        if any(not re.fullmatch(r"[a-z][a-z0-9_-]*", extra) for extra in extras):
            fail("Invalid wheel extras")
        # Read provenance only after checksum verification; never execute wheel contents here.
        with zipfile.ZipFile(wheel) as archive:
            manifests = [name for name in archive.namelist() if name.endswith(("/RELEASE.json", "/_build.json"))]
            provenance = json.loads(archive.read(manifests[0])) if manifests else {}
        rid = "v" + selected + "-" + digest[:12]
        target, record = self.environment(rid), self.root / "releases" / rid
        if target.exists() or record.exists() or target.is_symlink() or record.is_symlink():
            fail("Version environment already exists: " + rid)
        target.parent.mkdir(parents=True, exist_ok=True)
        record.parent.mkdir(parents=True, exist_ok=True)
        created = []
        try:
            target.mkdir()
            created.append(target)
            record.mkdir()
            created.append(record)
            self.run([sys.executable, "-m", "venv", str(target)], timeout=120)
            pip = [str(self.executable(rid, "python")), "-m", "pip", "install"]
            if offline:
                pip += ["--no-index", "--no-cache-dir", "--only-binary=:all:", "--find-links", str(wheel.parent)]
            self.run([*pip, str(wheel) + ("[" + ",".join(extras) + "]" if extras else "")])
            metadata = {"id": rid, "version": selected, "commit": provenance.get("commit") or commit,
                        "created": datetime.now(timezone.utc).isoformat(), "wheel_sha256": digest,
                        "extras": list(extras)}
            atomic_write(record / "release.json", json.dumps(metadata, indent=2) + "\n")
            return rid
        except BaseException:
            for path in reversed(created):
                shutil.rmtree(path)
            raise

    def write_shims(self, node: bool = False):
        scripts = self.prefix / "bin"
        scripts.mkdir(parents=True, exist_ok=True)
        for command in ["hydraweave"] + (["hydraweave-node"] if node else []):
            target = self.root / "current" / "bin" / command
            shim = "#!/bin/sh\nexec " + shlex.quote(str(target)) + ' "$@"\n'
            atomic_write(scripts / command, shim)
            (scripts / command).chmod(0o755)

    def apply_update(self, stage_fn, health_fn, *, report=print) -> dict:
        old, previous = self.current, self.previous
        report("Staging verified release")
        rid = stage_fn()
        report("Activating " + rid)
        try:
            self.activate(rid)
            report("Checking release health")
            result = health_fn(rid)
            if result is False:
                fail("health check returned false")
        except Exception as exc:
            reason = str(exc)
            if old:
                try:
                    self.activate(old)
                    atomic_write(self.root / "previous", previous or "")
                    report("Restoring previous release health")
                    if health_fn(old) is False:
                        fail("previous release health check returned false")
                except Exception as rollback:
                    reason += "; rollback health/recovery failed: " + str(rollback)
                state = "rolled_back" if self.current == old else "rollback_failed"
            else:
                (self.root / "current").unlink(missing_ok=True)
                state = "failed"
                reason += "; no previous release was installed"
            try:
                self.prune()
            except (OSError, ValueError, KeyError) as cleanup:
                reason += "; pruning failed: " + str(cleanup)
            result = {"state": state, "release": rid, "current": self.current, "reason": reason}
            report(state.replace("_", " ") + ": " + reason)
            return result
        result = {"state": "updated", "release": rid, "current": rid, "reason": None}
        try:
            self.prune()
        except (OSError, ValueError, KeyError) as cleanup:
            result["warning"] = "Updated, but pruning failed: " + str(cleanup)
            report(result["warning"])
        report("Updated to " + rid)
        return result


def portal_service(runner=None) -> str | None:
    if os.name == "nt":
        return None
    run = runner or subprocess.run
    for unit in ("hydraweave-portal.service", "hydraweave-ui.service", "hydraweave.service"):
        try:
            result = run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True, timeout=10)
            if result.returncode == 0 and result.stdout.strip() == "active":
                return unit
        except (OSError, subprocess.TimeoutExpired):
            return None
    return None


def http_health(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=2) as response:
        document = json.load(response) if response.status == 200 else {}
        return document if isinstance(document, dict) else {}


def health_check(manager: ReleaseManager, rid: str, *, service: str | None = None,
                 url: str = "http://127.0.0.1:18700/api/health", http=None, sleep=time.sleep, attempts: int = 30):
    manager.run([str(manager.executable(rid, "hydraweave")), "--version"], timeout=30)
    if service:
        record = manager.root / "releases" / rid / "release.json"
        expected = json.loads(record.read_text(encoding="utf-8")) if record.exists() else {}
        manager.run(["systemctl", "--user", "restart", service], timeout=60)
        for attempt in range(attempts):
            try:
                result = (http or http_health)(url)
                matches = result is True or (isinstance(result, dict) and
                    result.get("status") == "ok" and
                    (not expected.get("version") or result.get("version") == expected["version"]) and
                    (not expected.get("commit") or result.get("commit") == expected["commit"]))
                if result and matches:
                    return
            except (OSError, ValueError):
                pass
            if attempt + 1 < attempts:
                sleep(1)
        fail("Portal health endpoint did not recover: " + url)


def update(manager: ReleaseManager, *, wheel: Path | None = None, requested: str | None = None,
           check: bool = False, report=print, health_fn=None, service: str | None = None,
           url: str = "http://127.0.0.1:18700/api/health", cancelled=lambda: False) -> dict:
    if wheel:
        wheel = Path(wheel).expanduser().absolute()
        selected = wheel_version(wheel.name)
        if requested and version(requested) != selected:
            fail("Wheel version does not match --version")
        entries = checksums(wheel.with_name("SHA256SUMS").read_bytes())
        verify(wheel, entries)
        assets = None
    else:
        try:
            selected, name, assets = release(requested)
        except NoPublishedRelease as exc:
            report(str(exc))
            return {"state": "no_published_release", "reason": str(exc)}
    if check:
        report("Published release: v" + selected)
        return {"state": "available", "version": selected}
    service = service or portal_service(manager.runner)
    health_fn = health_fn or (lambda rid: health_check(manager, rid, service=service, url=url))
    with operation_lock(manager.root), tempfile.TemporaryDirectory(prefix="hydraweave-download-") as directory:
        if cancelled():
            fail("Update cancelled before staging")
        if assets is not None:
            wheel = Path(directory) / name
            (wheel.parent / "SHA256SUMS").write_bytes(read_url(assets["SHA256SUMS"]))
            wheel.write_bytes(read_url(assets[name]))
            verify(wheel, checksums(wheel.with_name("SHA256SUMS").read_bytes()))
        digest = verify(wheel, checksums(wheel.with_name("SHA256SUMS").read_bytes()))
        expected = "v" + selected + "-" + digest[:12]
        if manager.current == expected:
            report("Already up to date: " + expected)
            return {"state": "up_to_date", "current": expected, "reason": None}
        extras = next((r.get("extras", []) for r in manager.list() if r["current"]), [])
        def stage():
            rid = manager.stage(wheel, extras, offline=assets is None)
            if cancelled():
                manager.remove(rid)
                fail("Update cancelled before activation")
            return rid
        return manager.apply_update(stage, health_fn, report=report)


def update_status(manager: ReleaseManager) -> dict:
    try:
        job = identifier((manager.root / "update-latest").read_text(encoding="utf-8").strip())
        return json.loads((manager.root / "update-jobs" / (job + ".json")).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def persist_update(manager: ReleaseManager, job: str, *, report=print, **kwargs) -> dict:
    path = manager.root / "update-jobs" / (identifier(job) + ".json")
    def progress(text):
        atomic_write(path, json.dumps({"job": job, "state": "running", "progress": text}))
        report(text)
    try:
        result = update(manager, report=progress, **kwargs)
    except Exception as exc:
        result = {"state": "failed", "reason": str(exc)}
    atomic_write(path, json.dumps(dict(result, job=job)))
    report(result["state"].replace("_", " ") + (": " + result["reason"] if result.get("reason") else ""))
    return result


def portal_update(manager: ReleaseManager, *, report=print, cancelled=lambda: False,
                  url: str = "http://127.0.0.1:18700/api/health", sleep=time.sleep) -> dict:
    """Move a service-owned update outside the portal's cgroup before restarting it.

    The durable result is displayed after reconnection even if the in-memory job
    disappears when systemd stops the old portal.
    """
    if cancelled():
        fail("Update cancelled")
    job = uuid.uuid4().hex
    path = manager.root / "update-jobs" / (job + ".json")
    atomic_write(path, json.dumps({"job": job, "state": "running", "progress": "Starting update"}))
    atomic_write(manager.root / "update-latest", job)
    service = portal_service(manager.runner)
    if not service:
        return persist_update(manager, job, report=report, cancelled=cancelled, url=url)
    command = ["systemd-run", "--user", "--collect", "--unit=hydraweave-update-" + job]
    if os.environ.get("HYDRAWEAVE_RELEASE_API"):
        command.append("--setenv=HYDRAWEAVE_RELEASE_API=" + os.environ["HYDRAWEAVE_RELEASE_API"])
    command += ["--", sys.executable, str(Path(__file__).absolute()), "--worker", "--prefix", str(manager.prefix),
                "--job", job, "--service", service, "--health-url", url]
    try:
        manager.run(command, timeout=30)
    except Exception as exc:
        result = {"job": job, "state": "failed", "reason": "Could not start update worker: " + str(exc)}
        atomic_write(path, json.dumps(result))
        return result
    # Cancellation after handoff must not interrupt activation/automatic rollback.
    for _ in range(2400):
        result = json.loads(path.read_text(encoding="utf-8"))
        if result["state"] != "running":
            return result
        report(result["progress"])
        sleep(1)
    fail("Update worker did not finish; inspect the user unit and the persisted update status")


def configure_cli(commands):
    releases = commands.add_parser("releases", help="list, activate or remove installed releases")
    releases.add_argument("--prefix", type=Path)
    operations = releases.add_subparsers(dest="operation", required=True)
    operations.add_parser("list")
    for name in ("activate", "remove"):
        operations.add_parser(name).add_argument("id")
    updater = commands.add_parser("update", help="update a verified release with automatic rollback")
    updater.add_argument("--check", action="store_true")
    updater.add_argument("--wheel", type=Path)
    updater.add_argument("--version")
    updater.add_argument("--prefix", type=Path)


def run_cli(args) -> int:
    manager = ReleaseManager(args.prefix)
    if args.command == "update":
        result = update(manager, wheel=args.wheel, requested=args.version, check=args.check)
        return 1 if result["state"] in {"rolled_back", "rollback_failed", "failed"} else 0
    if args.operation == "list":
        for row in manager.list():
            mark = "current" if row["current"] else "previous" if row["previous"] else ""
            print(f"{row['id']}\t{row['version']}\t{row.get('commit') or 'unknown'}\t{mark}")
    else:
        with operation_lock(manager.root):
            getattr(manager, args.operation)(args.id)
        print(args.operation.capitalize() + ": " + args.id)
    return 0


def install(args, selected: str, wheel: Path, offline: bool):
    manager = ReleaseManager(args.prefix)
    print("Install " + wheel.name + " into " + str(manager.root / "venvs" / ("v" + selected + "-<sha256>")))
    print("Node agent extras: " + ("yes" if args.node else "no"))
    for command in ["hydraweave"] + (["hydraweave-node"] if args.node else []):
        print("Write shim: " + str(manager.prefix / "bin" / command))
    if args.dry_run:
        print("Dry run: no downloads, venv, package installs or shims written.")
        return
    with operation_lock(manager.root):
        result = manager.apply_update(lambda: manager.stage(wheel, ["node"] if args.node else [], offline=offline),
                                      lambda rid: health_check(manager, rid))
        if result["state"] != "updated":
            fail(result["reason"])
        manager.write_shims(args.node)
    scripts = manager.prefix / "bin"
    if str(scripts) not in os.environ.get("PATH", "").split(os.pathsep):
        print("Add to your shell profile: export PATH=" + shlex.quote(str(scripts)) + ':"$PATH"')
    print("Installed Hydraweave v" + selected + ". Run hydraweave --help.")
    if args.node:
        print("Start the node agent with hydraweave-node --help; pairing and services are configured separately.")


def installer_main():
    parser = argparse.ArgumentParser(description="Install Hydraweave from a verified GitHub release wheel")
    parser.add_argument("--version")
    parser.add_argument("--node", action="store_true")
    parser.add_argument("--prefix", type=Path, default=Path.home() / ".local")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--from-file", type=Path)
    args = parser.parse_args()
    check_python(args.node)
    if args.from_file:
        wheel = args.from_file.expanduser().absolute()
        selected = wheel_version(wheel.name)
        if args.version and version(args.version) != selected:
            fail("Wheel version does not match --version")
        entries = checksums(wheel.with_name("SHA256SUMS").read_bytes())
        verify(wheel, entries)
        for dependency in wheel.parent.glob("*.whl"):
            verify(dependency, entries)
        install(args, selected, wheel, offline=True)
        return
    selected, name, assets = release(args.version)
    if args.dry_run:
        install(args, selected, Path(name), offline=False)
        return
    with tempfile.TemporaryDirectory(prefix="hydraweave-download-") as directory:
        wheel = Path(directory) / name
        (wheel.parent / "SHA256SUMS").write_bytes(read_url(assets["SHA256SUMS"]))
        wheel.write_bytes(read_url(assets[name]))
        verify(wheel, checksums(wheel.with_name("SHA256SUMS").read_bytes()))
        install(args, selected, wheel, offline=False)


def main():
    if "--worker" not in sys.argv[1:]:
        return installer_main()
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--health-url", required=True)
    args = parser.parse_args()
    persist_update(ReleaseManager(args.prefix), args.job, service=args.service, url=args.health_url)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        print("hydraweave install: " + str(exc), file=sys.stderr)
        sys.exit(1)
PY
