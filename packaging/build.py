#!/usr/bin/env python3
"""Build local .deb/.rpm with fpm; --dry-run needs no fpm, network or root."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def command(kind, stage, output):
    recipe = json.loads((ROOT / "packaging/recipes.json").read_text())[kind]
    result = ["fpm", "-s", "dir", "-t", kind, "--name", "hydraweave-node", "--version", "0.1.0",
              "--architecture", "all" if kind == "deb" else "noarch", "--license", "Apache-2.0",
              "--description", "Unprivileged Hydraweave node discovery agent", "--prefix", "/",
              "--chdir", str(stage), "--package", str(output)]
    for dependency in recipe["depends"]:
        result += ["--depends", dependency]
    return result + ["usr"]


def build(kind, directory, dry_run=False):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    # A fresh stage also excludes files removed since an earlier build, without deleting any user files.
    stage = Path(tempfile.mkdtemp(prefix="stage-" + kind + "-", dir=directory))
    package = stage / "usr/lib/hydraweave"
    for name in ("hydraweave", "node"):
        for source in sorted((ROOT / name).rglob("*")):
            if not source.is_file() or source.is_symlink() or "__pycache__" in source.parts:
                continue
            target = package / source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    shutil.copyfile(ROOT / "versions.lock", package / "versions.lock")
    licenses = stage / "usr/share/licenses/hydraweave-node"
    licenses.mkdir(parents=True, exist_ok=True)
    for name in ("LICENSE", "NOTICE"):
        shutil.copyfile(ROOT / name, licenses / name)
    wrapper = stage / "usr/bin/hydraweave-node"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text('#!/bin/sh\nPYTHONPATH=/usr/lib/hydraweave${PYTHONPATH:+:$PYTHONPATH} exec /usr/bin/python3 -m hydraweave.node_agent "$@"\n')
    wrapper.chmod(0o755)
    unit = stage / "usr/lib/systemd/user/hydraweave-node.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "packaging/hydraweave-node.service", unit)
    args = command(kind, stage, directory / ("hydraweave-node." + kind))
    if not dry_run:
        subprocess.run(args, check=True)
    return {"command": args, "stage": str(stage), "dry_run": dry_run}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("deb", "rpm"), required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/packages")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(build(args.format, args.output, args.dry_run), indent=2))


if __name__ == "__main__":
    main()
