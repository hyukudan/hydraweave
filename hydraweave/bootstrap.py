"""Controller-served source package and POSIX bootstrap; never executes privileged commands."""
import io
from pathlib import Path
import tarfile
from urllib.parse import urlsplit

from .paths import repo_root


def script(origin):
    parsed = urlsplit(origin)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
            or parsed.path not in {"", "/"} or any(c in origin for c in "'\n\r`$\\\"")):
        raise ValueError("invalid controller origin")
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
mkdir -p "$root/etc" "$root/incoming" "$root/lab-arm" "$root/log" "$root/releases"
if [ ! -x "$root/agent-venv/bin/python" ]; then
    "$python" -m venv "$root/agent-venv"
fi
"$root/agent-venv/bin/python" -m pip install --upgrade --force-reinstall 'hydraweave[node] @ """ + origin.rstrip("/") + """/node/package.tar.gz'
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


def source_package():
    """Build from the checkout's allow-listed package resources, without executing a build backend."""
    root = repo_root()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name in ("pyproject.toml", "versions.lock", "README.md", "LICENSE", "NOTICE", "hydraweave", "hyperloom", "node"):
            base = (Path(__file__).parent if name == "hydraweave" else
                    Path(__file__).parent.parent / "hyperloom" if name == "hyperloom" else root / name)
            paths = [base] if base.is_file() else sorted(base.rglob("*"))
            for path in paths:
                if (path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts
                        and path.suffix not in {".pyc", ".pyo"}):
                    relative = name if base.is_file() else name + "/" + path.relative_to(base).as_posix()
                    archive.add(path, arcname="hydraweave/" + relative, recursive=False)
    return stream.getvalue()
