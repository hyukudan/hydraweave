"""Cross-transport exclusion while the node agent changes releases."""
import json
from pathlib import Path

VERBS = set()
PENDING = {"staging", "staged", "prepared", "activating", "rolling_back", "rollback_failed"}
TERMINAL = {"ready", "rolled_back", "failed"}


def require_idle(root):
    """Call under the node's root.lock before publishing new work, never for stop/status."""
    path = Path(root) / "run/agent-update.json"
    if path.is_symlink():
        raise ValueError("agent update receipt must not be a symlink")
    if not path.exists():
        return
    with path.open(encoding="utf-8") as stream:
        raw = stream.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        raise ValueError("agent update receipt is too large")
    row = json.loads(raw)
    if not isinstance(row, dict) or row.get("state") not in PENDING | TERMINAL:
        raise ValueError("invalid agent update receipt")
    if row["state"] in PENDING:
        raise ValueError("Node update in progress; new work is refused until activation or rollback is verified")
