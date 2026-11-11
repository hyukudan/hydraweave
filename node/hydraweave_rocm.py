"""ROCm discovery shared by node inspection, builds and runtime manifests."""

from collections.abc import Mapping
import os
from pathlib import Path
import re

VERBS = frozenset()  # helper module, no agent verbs


def _version(path: Path) -> tuple[tuple[int, ...], str]:
    return tuple(int(part) for part in re.findall(r"\d+", path.name)), str(path)


def detect_rocm(environ: Mapping[str, str] | None = None,
                root: Path | None = None) -> Path | None:
    """Prefer ROCM_PATH, /opt/rocm, then the highest numbered installation.

    ``root`` maps absolute paths into a fixture tree; no host paths are probed
    when it is supplied. Missing overrides fall through to installed versions.
    """
    env = os.environ if environ is None else environ

    def mapped(path: Path) -> Path:
        return root / path.as_posix().lstrip("/") if root is not None else path

    override = env.get("ROCM_PATH")
    if override:
        candidate = Path(override)
        if candidate.is_absolute() and mapped(candidate).is_dir():
            return mapped(candidate)
    opt = mapped(Path("/opt"))
    standard = opt / "rocm"
    # Some distributions use /opt/rocm as a parent of core-* rather than
    # the usual SDK symlink. In that layout choose a versioned core below it.
    cores = list(standard.glob("core-*"))
    if standard.is_dir() and (standard.is_symlink() or not cores
                              or (standard / "bin").is_dir() or (standard / "lib").is_dir()):
        return standard
    candidates = [p for p in (*opt.glob("rocm-*"), *cores) if p.is_dir()]
    return max(candidates, key=_version) if candidates else None


def runtime_environment(rocm: Path) -> dict[str, str]:
    libraries = [str(rocm / "lib"), str(rocm / "lib64")]
    return {"LD_LIBRARY_PATH": ":".join(libraries)}


if __name__ == "__main__":
    found = detect_rocm()
    if found is None:
        raise SystemExit("ROCm not found; set ROCM_PATH to an installed ROCm SDK")
    print(found)
