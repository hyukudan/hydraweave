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
import argparse
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
import urllib.error
import urllib.parse
import urllib.request


def fail(message):
    raise ValueError(message)


def version(value):
    value = value.removeprefix("v")
    if not re.fullmatch(r"[0-9][A-Za-z0-9.!+_-]*", value):
        fail("Invalid release version")
    return value


def wheel_version(name):
    match = re.fullmatch(r"hydraweave-([^-]+)-py3-none-any\.whl", name)
    if not match:
        fail("Expected a hydraweave-<version>-py3-none-any.whl wheel")
    return version(match[1])


def read_url(url):
    request = urllib.request.Request(url, headers={"User-Agent": "hydraweave-installer",
                                                 "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def checksums(data):
    entries = {}
    for line in data.decode("utf-8").splitlines():
        match = re.fullmatch(r"([a-fA-F0-9]{64}) [ *]([^/\\]+)", line)
        if not match or match[2] in entries:
            fail("Malformed or duplicate SHA256SUMS entry")
        entries[match[2]] = match[1].lower()
    return entries


def verify(wheel, entries):
    expected = entries.get(wheel.name)
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    if expected is None or digest != expected:
        fail("SHA256 mismatch or missing checksum: " + wheel.name)


def release(requested):
    # Override supports mirrors and local file:// release metadata for tests.
    api = os.environ.get("HYDRAWEAVE_RELEASE_API",
                         "https://api.github.com/repos/hyukudan/hydraweave/releases").rstrip("/")
    endpoint = "tags/" + urllib.parse.quote("v" + version(requested), safe="") if requested else "latest"
    try:
        metadata = json.loads(read_url(api + "/" + endpoint))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            fail("No matching GitHub release exists yet; releases are produced by pushing a v* tag. "
                 "Use a source checkout until a release is published.")
        raise
    selected = version(metadata["tag_name"])
    if requested and selected != version(requested):
        fail("Release metadata does not match --version")
    assets = {asset["name"]: asset["browser_download_url"] for asset in metadata["assets"]}
    wheel = "hydraweave-" + selected + "-py3-none-any.whl"
    if wheel not in assets or "SHA256SUMS" not in assets:
        fail("Release must contain the wheel and SHA256SUMS")
    return selected, wheel, assets


def check_python(node=False):
    # The node agent supports 3.10; the controller uses 3.11 stdlib features.
    floor = (3, 10) if node else (3, 11)
    if sys.version_info < floor:
        fail("Python %d.%d or newer is required%s" % (floor + (" for --node" if node else ";"
             " node-only installs (--node) accept 3.10",)))
    if any(importlib.util.find_spec(module) is None for module in ("venv", "ensurepip")):
        fail("Python venv support is missing. On Debian/Ubuntu, run exactly: "
             "sudo apt-get install python3-venv")


def install(args, selected, wheel, offline):
    prefix = args.prefix.expanduser().absolute()
    target = prefix / "share" / "hydraweave" / "venvs" / ("v" + selected)
    scripts = prefix / "bin"
    commands = ["hydraweave"] + (["hydraweave-node"] if args.node else [])
    print("Install " + wheel.name + " into " + str(target))
    print("Node agent extras: " + ("yes" if args.node else "no"))
    for command in commands:
        print("Write shim: " + str(scripts / command))
    if args.dry_run:
        print("Dry run: no downloads, venv, package installs or shims written.")
        return
    if target.exists():
        fail("Version environment already exists: " + str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run([sys.executable, "-m", "venv", str(target)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode:
            fail("Could not create venv: " + result.stdout.strip() + " " + result.stderr.strip())
        requirement = str(wheel) + ("[node]" if args.node else "")
        pip = [str(target / "bin" / "python"), "-m", "pip", "install"]
        if offline:
            pip += ["--no-index", "--no-cache-dir", "--only-binary=:all:", "--find-links", str(wheel.parent)]
        subprocess.run([*pip, requirement], check=True)
    except BaseException:
        if target.is_dir():
            shutil.rmtree(target)
        raise
    scripts.mkdir(parents=True, exist_ok=True)
    for command in commands:
        shim = "#!/bin/sh\nexec " + shlex.quote(str(target / "bin" / command)) + ' "$@"\n'
        fd, temporary = tempfile.mkstemp(prefix=".hydraweave-", dir=scripts)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(shim)
            os.chmod(temporary, 0o755)
            os.replace(temporary, scripts / command)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    if str(scripts) not in os.environ.get("PATH", "").split(os.pathsep):
        print("Add to your shell profile: export PATH=" + shlex.quote(str(scripts)) + ':"$PATH"')
    print("Installed Hydraweave v" + selected + ". Run hydraweave --help.")
    if args.node:
        print("Start the node agent with hydraweave-node --help; pairing and services are configured separately.")


def main():
    parser = argparse.ArgumentParser(description="Install Hydraweave from a verified GitHub release wheel")
    parser.add_argument("--version", help="release tag, for example v0.1.0 (default: latest)")
    parser.add_argument("--node", action="store_true", help="also install node extras and a node agent shim")
    parser.add_argument("--prefix", type=Path, default=Path.home() / ".local")
    parser.add_argument("--dry-run", action="store_true", help="resolve and show the install without writing")
    parser.add_argument("--from-file", type=Path, help="offline wheel with adjacent SHA256SUMS and dependency wheels")
    args = parser.parse_args()
    check_python(args.node)
    if args.from_file:
        wheel = args.from_file.expanduser().absolute()
        selected = wheel_version(wheel.name)
        if args.version and version(args.version) != selected:
            fail("Wheel version does not match --version")
        entries = checksums(wheel.with_name("SHA256SUMS").read_bytes())
        verify(wheel, entries)
        # pip may consume sibling dependency wheels in offline mode; verify them too.
        for dependency in wheel.parent.glob("*.whl"):
            verify(dependency, entries)
        install(args, selected, wheel, offline=True)
        return
    selected, name, assets = release(args.version)
    if args.dry_run:
        install(args, selected, Path(name), offline=False)
        return
    entries = checksums(read_url(assets["SHA256SUMS"]))
    with tempfile.TemporaryDirectory(prefix="hydraweave-download-") as directory:
        wheel = Path(directory) / name
        wheel.write_bytes(read_url(assets[name]))
        verify(wheel, entries)
        install(args, selected, wheel, offline=False)


try:
    main()
except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as exc:
    print("hydraweave install: " + str(exc), file=sys.stderr)
    sys.exit(1)
PY
