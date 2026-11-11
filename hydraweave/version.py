"""Runtime version and source revision discovery.

One source of truth: the ``version`` in pyproject.toml. Checkouts and release trees
read it from pyproject.toml; installed wheels read it from package metadata. The
commit comes from RELEASE.json (release trees built by tools/make_release.py) or
``git rev-parse`` (checkouts).
"""
from __future__ import annotations

from importlib import metadata
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
UNKNOWN = "0.0.0+unknown"


def release_manifest(root=ROOT):
    """RELEASE.json of a release tree as a dict, or {} outside release trees."""
    try:
        data = json.loads((Path(root) / "RELEASE.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def package_version(root=ROOT):
    """pyproject.toml version, then installed metadata, then RELEASE.json, else UNKNOWN."""
    try:
        text = (Path(root) / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if match:
            return match.group(1)
    except OSError:
        pass
    try:
        return metadata.version("hydraweave")
    except metadata.PackageNotFoundError:
        pass
    value = release_manifest(root).get("version")
    return value if isinstance(value, str) and value else UNKNOWN


def source_commit(root=ROOT):
    """Short commit of this tree (git checkout or RELEASE.json), or None."""
    try:
        commit = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=root,
                                capture_output=True, text=True, timeout=2, check=True).stdout.strip()
        if commit:
            return commit
    except (OSError, subprocess.SubprocessError):
        pass
    commit = release_manifest(root).get("commit")
    return commit[:12] if isinstance(commit, str) and commit else None


def version_info(root=ROOT):
    return package_version(root), source_commit(root)


def _release(value):
    # Local/dev and git-describe suffixes describe packaging context; compare releases only.
    value = re.sub(r"-\d+-g[0-9a-f]+(?:-dirty)?$", "", value.strip())
    return re.sub(r"[.+-](?:dev|local).*$|\+.*$", "", value, flags=re.IGNORECASE).lstrip("vV")


def version_status(agent_version, portal_version=None):
    """'ok', 'outdated' or 'unknown' for an agent version compared with the portal's."""
    if not isinstance(agent_version, str) or not agent_version.strip():
        return "unknown"
    portal_version = portal_version or package_version()
    if portal_version == UNKNOWN:
        return "unknown"
    return "ok" if _release(agent_version) == _release(portal_version) else "outdated"
