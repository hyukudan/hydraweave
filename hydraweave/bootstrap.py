"""Controller-served source package and POSIX bootstrap; never executes privileged commands."""
import io
import hashlib
from pathlib import Path
import re
import tarfile
import threading
from urllib.parse import urlsplit

from .paths import repo_root


def script(origin: str, sha256: str | None = None) -> bytes:
    parsed = urlsplit(origin)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
            or parsed.path not in {"", "/"} or any(c in origin for c in "'\n\r`$\\\"")):
        raise ValueError("invalid controller origin")
    sha256 = package_sha256() if sha256 is None else sha256
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("invalid package sha256")
    return ("""#!/bin/sh
set -eu
umask 077
root=${HYDRAWEAVE_ROOT:-"$HOME/hydraweave"}
python=${HYDRAWEAVE_PYTHON:-python3}
missing=''
packages=''
for package in python3-venv curl git cmake ninja-build dkms "linux-headers-$(uname -r)"; do
    dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'install ok installed' || packages="$packages $package"
done
[ -z "$packages" ] || missing="$missing\\ninstall apt packages:$packages"
groups=$(id -nG)
group_missing=0
case " $groups " in *' render '*) ;; *) group_missing=1 ;; esac
case " $groups " in *' video '*) ;; *) group_missing=1 ;; esac
[ "$group_missing" = 0 ] || missing="$missing\\nadd render/video group access"
kfd_missing=0; [ -r /dev/kfd ] || { kfd_missing=1; missing="$missing\\nrestore /dev/kfd access (udev permissions)"; }
driver_missing=0; [ -d /sys/module/amdgpu ] || { driver_missing=1; missing="$missing\\nload/install amdgpu using AMD's installer"; }
rocm_missing=0; [ -d /opt/rocm ] || { rocm_missing=1; missing="$missing\\ninstall ROCm userspace matching the selected engine recipe"; }
if [ -n "$missing" ]; then
    printf '%b\\n' 'Missing node prerequisites:' "$missing"
    [ -z "$packages" ] || printf 'Root step: sudo apt-get update && sudo apt-get install -y%s\\n' "$packages"
    [ "$group_missing" = 0 ] || printf 'Root step: sudo usermod -aG render,video %s\\n' "$USER"
    [ "$kfd_missing" = 0 ] || printf '%s\\n' 'Configure /dev/kfd udev access, then re-login.'
    if [ "$driver_missing" = 1 ] || [ "$rocm_missing" = 1 ]; then
        printf '%s\\n' 'AMD DKMS/ROCm steps: https://rocm.docs.amd.com/projects/install-on-linux/en/latest/'
    fi
fi
"$python" -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required"'
expected=${HYDRAWEAVE_PKG_SHA256:-}
if [ -z "$expected" ]; then
    expected='""" + sha256 + """'
    printf '%s\\n' 'Note: HYDRAWEAVE_PKG_SHA256 is unset; using the SHA-256 embedded in this script.' >&2
fi
package_dir=$(mktemp -d)
trap 'rm -rf "$package_dir"' EXIT
trap 'exit 1' HUP INT TERM
package="$package_dir/package.tar.gz"
curl -fsSL '""" + origin.rstrip("/") + """/node/package.tar.gz' -o "$package"
if command -v sha256sum >/dev/null 2>&1; then
    actual=$(sha256sum "$package")
elif command -v shasum >/dev/null 2>&1; then
    actual=$(shasum -a 256 "$package")
else
    actual=$(python3 -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' "$package")
fi
actual=${actual%% *}
if [ "$actual" != "$expected" ]; then
    printf '%s\\n' "Package SHA-256 mismatch: expected $expected, got $actual; installation aborted." >&2
    exit 1
fi
mkdir -p "$root/etc" "$root/incoming" "$root/lab-arm" "$root/log" "$root/releases"
if [ ! -x "$root/agent-venv/bin/python" ]; then
    "$python" -m venv "$root/agent-venv"
fi
"$root/agent-venv/bin/python" -m pip install --upgrade --force-reinstall "hydraweave[node] @ file://$package"
if [ -n "${HYDRAWEAVE_LAB_DIR:-}" ]; then
    "$root/agent-venv/bin/hydraweave-node" install-service --root "$root" --lab-dir "$HYDRAWEAVE_LAB_DIR"
else
    "$root/agent-venv/bin/hydraweave-node" install-service --root "$root"
fi
printf '%s\\n' 'Agent installed.'
attempt=0
until "$root/agent-venv/bin/hydraweave-node" pair 2>/dev/null; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 15 ]; then
        printf '%s\\n' 'Agent is still starting; run hydraweave-node pair to show the pairing code.'
        break
    fi
    sleep 1
done
printf '%s\\n' 'Reissue pairing code: hydraweave-node pair'
""").encode()


_cache_lock = threading.Lock()
_cache_key: tuple = ()
_cache_package = b""
_cache_sha256 = ""


def _sources() -> list[tuple[Path, str]]:
    root = repo_root()
    sources = []
    for name in ("pyproject.toml", "versions.lock", "README.md", "LICENSE", "NOTICE", "hydraweave", "hyperloom", "node"):
        base = (Path(__file__).parent if name == "hydraweave" else
                Path(__file__).parent.parent / "hyperloom" if name == "hyperloom" else root / name)
        paths = [base] if base.is_file() else sorted(base.rglob("*"))
        for path in paths:
            if (path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts
                    and path.suffix not in {".pyc", ".pyo"}):
                relative = name if base.is_file() else name + "/" + path.relative_to(base).as_posix()
                sources.append((path, "hydraweave/" + relative))
    return sources


def package() -> tuple[bytes, str]:
    """Cache the exact archive and its digest together until a source mtime/size or file list changes."""
    global _cache_key, _cache_package, _cache_sha256
    with _cache_lock:
        sources = _sources()
        key = tuple((str(path), name, stat.st_mtime_ns, stat.st_size)
                    for path, name in sources for stat in [path.stat()])
        if key != _cache_key or not _cache_package:
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w:gz") as archive:
                for path, name in sources:
                    archive.add(path, arcname=name, recursive=False)
            data = stream.getvalue()
            _cache_key, _cache_package, _cache_sha256 = key, data, hashlib.sha256(data).hexdigest()
        return _cache_package, _cache_sha256


def source_package() -> bytes:
    """Build only allow-listed resources, without executing a build backend."""
    return package()[0]


def package_sha256() -> str:
    return package()[1]
