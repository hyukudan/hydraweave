"""Small, data oriented backend description shared by controller features."""
from dataclasses import dataclass
from pathlib import Path
import json


@dataclass(frozen=True)
class Backend:
    name: str
    families: tuple[str, ...]
    topologies: tuple[str, ...]
    requires: tuple[str, ...]
    ready: dict
    drain: str
    api: frozenset[str]
    adapter: dict
    family_models: tuple[str, str]
    placement_engines: tuple[str, ...] = ()
    def presets(self):
        return tuple(sorted((Path(__file__).resolve().parent.parent / "presets").glob("*.json")))
    def processes(self, profile, members):
        return None


def load_json_records(directory):
    rows = {}
    for path in sorted(Path(directory).glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or not isinstance(doc.get("id"), str) or not doc["id"]:
            raise ValueError(f"{path.name}: model id must be nonempty text")
        if not isinstance(doc.get("family", doc.get("backend")), str):
            raise ValueError(f"{path.name}: family or backend is required")
        files = doc.get("files")
        if not isinstance(files, list):
            raise ValueError(f"{path.name}: files must be a list")
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]:
                raise ValueError(f"{path.name}: each file needs a name")
            if "bytes" in item and (type(item["bytes"]) is not int or item["bytes"] < 0):
                raise ValueError(f"{path.name}: file bytes must be a nonnegative integer")
            if "sha256" in item and (not isinstance(item["sha256"], str) or
                                      not __import__("re").fullmatch(r"[0-9a-f]{64}", item["sha256"])):
                raise ValueError(f"{path.name}: sha256 must be 64 lowercase hex characters")
        rows[doc["id"]] = doc
    return rows
