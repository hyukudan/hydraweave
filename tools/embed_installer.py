"""Refresh the standalone installer from the stdlib release manager."""
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    installer = root / "install.sh"
    header = installer.read_text(encoding="utf-8").split("<<'PY'\n", 1)[0]
    source = (root / "hydraweave" / "releases.py").read_text(encoding="utf-8")
    installer.write_text(header + "<<'PY'\n" + source.rstrip() + "\nPY\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
