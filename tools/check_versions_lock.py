#!/usr/bin/env python3
"""Compare versions.lock component commits with the checked out submodule gitlinks."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PATHS = {"tensormeld": "third_party/tensormeld", "engine": "third_party/engine",
         "strix-trellis": "third_party/strix-trellis"}


def mismatches():
    lock = json.loads((ROOT / "versions.lock").read_text(encoding="utf-8"))
    result = subprocess.run(["git", "ls-tree", "HEAD", "third_party/"], cwd=ROOT,
                            capture_output=True, text=True, check=True)
    gitlinks = {line.split("\t", 1)[1]: line.split()[2] for line in result.stdout.splitlines() if "\t" in line}
    return [(name, lock["components"][name]["commit"], gitlinks.get(path))
            for name, path in PATHS.items() if lock["components"][name]["commit"] != gitlinks.get(path)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="exit nonzero for any mismatch")
    args = parser.parse_args(argv)
    drift = mismatches()
    if not drift:
        print("versions.lock matches all third_party gitlinks")
        return 0
    for name, locked, gitlink in drift:
        message = f"versions.lock drift: {name} lock={locked} gitlink={gitlink or 'missing'}"
        print(f"::error::{message}" if args.strict else f"::warning::{message}")
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
