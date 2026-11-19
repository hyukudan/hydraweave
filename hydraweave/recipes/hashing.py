"""Bounded-memory hashes for explicit release-binary identity checks, including Python 3.10."""
from pathlib import Path
import hashlib


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()
