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
cat <<'PRIVILEGES'
If prerequisites are missing, review and run this block yourself, then rerun bootstrap.
The agent never runs this block. amdgpu-dkms requires your approved AMD apt repository.
sudo sh -eu <<'ROOT'
apt-get update
apt-get install -y python3-venv curl git cmake ninja-build dkms "linux-headers-$(uname -r)" amdgpu-dkms
usermod -aG render,video "${SUDO_USER:?run sudo from your node login}"
printf '%s\\n' 'SUBSYSTEM=="drm", KERNEL=="renderD*", GROUP="render", MODE="0660"' 'KERNEL=="tbstream*", GROUP="render", MODE="0660"' > /etc/udev/rules.d/70-hydraweave-render.rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=drm
loginctl enable-linger "${SUDO_USER}"
ROOT
Reboot after a driver change; log out and in after a group change.
ROCm userspace/compiler must match the selected engine recipe (see Components).
PRIVILEGES
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
printf '%s\\n' 'Agent installed. Pairing code: journalctl --user -u hydraweave-node -n 20 --no-pager'
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
