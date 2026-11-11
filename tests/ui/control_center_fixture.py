"""Isolated HTTP fixture for control_center_dom.cjs.

Configuration reads/writes, validation, CSRF, static files and API responses are real.
Node observations are unavailable test inputs; this fixture never contacts hardware.
"""
from contextlib import ExitStack
import os
from pathlib import Path
import signal
import tempfile
from unittest.mock import patch

from hydraweave import orchestrate, remote, secrets
from hydraweave.web.api import API
from hydraweave.web.features import ops
from hydraweave.web.server import create_server


def no_remote(*args, **kwargs):
    raise RuntimeError("Control center DOM fixture: hardware access is disabled")


def shutdown(_signum, _frame):
    raise KeyboardInterrupt


def main():
    signal.signal(signal.SIGTERM, shutdown)
    with tempfile.TemporaryDirectory(prefix="hydraweave-control-center-dom-") as temporary, ExitStack() as stack:
        root = Path(temporary)
        os.environ["XDG_CONFIG_HOME"] = str(root / "config")
        os.environ["XDG_DATA_HOME"] = str(root / "data")
        os.environ["HYDRAWEAVE_COMPONENT_CACHE"] = str(root / "components")
        stack.enter_context(patch.object(secrets, "runtime_dir", lambda: root / "run"))
        stack.enter_context(patch.object(secrets, "_directory", lambda: root / "secrets"))
        stack.enter_context(patch.object(orchestrate, "status", lambda *a, **k: {}))
        stack.enter_context(patch.object(orchestrate, "_absent", lambda *a, **k: False))
        stack.enter_context(patch.object(ops, "_node_call", lambda *a, **k: {"leases": []}))
        stack.enter_context(patch.object(remote, "keyscan", lambda *a, **k: []))
        stack.enter_context(patch.object(remote.Remote, "call", no_remote))
        stack.enter_context(patch.object(API, "_probe", lambda *a, **k: None))
        stack.enter_context(patch.object(API, "_node", lambda *a, **k: {
            "leases": [], "why": "DOM fixture: no hardware observation"
        }))
        api = API(root / "config" / "cluster.json", audit_path=root / "audit.jsonl")
        with create_server(0, api=api) as server:
            print("URL=" + server.origin, flush=True)
            try:
                server.serve_forever(poll_interval=.2)
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
