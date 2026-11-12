import logging
from pathlib import Path

import pytest

from hydraweave import store
from hydraweave.log import setup


def test_atomic_write_retries_permission_error(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    real_replace = store.os.replace
    calls = []

    def replace(source, destination):
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError("sharing")
        return real_replace(source, destination)

    monkeypatch.setattr(store.os, "replace", replace)
    store.write_json(target, {"ok": True}, mode=0o600)
    assert store.read_json(target) == {"ok": True}
    assert len(calls) == 2


def test_atomic_write_keeps_existing_file_after_replace_failure(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_text('{"old": true}\n', encoding="utf-8")
    monkeypatch.setattr(store.os, "replace", lambda *_: (_ for _ in ()).throw(PermissionError("busy")))
    with pytest.raises(PermissionError):
        store.write_json(target, {"new": True})
    assert store.read_json(target) == {"old": True}


def test_log_setup_redacts_token_values_by_not_logging_them(tmp_path):
    path = tmp_path / "hydraweave.log"
    setup("DEBUG", json=True, file=path)
    logger = logging.getLogger("hydraweave.test")
    logger.info("authorization: Bearer token-secret")
    logging.getLogger("hydraweave").handlers[-1].flush()
    output = path.read_text(encoding="utf-8")
    assert "token-secret" not in output
    assert '"logger": "hydraweave.test"' in output


def test_transport_factory_selects_agent(monkeypatch, tmp_path):
    from hydraweave import transport

    class AgentClient:
        def __init__(self, machine, path):
            self.machine, self.path = machine, path

    monkeypatch.setattr("hydraweave.onboarding.Client", AgentClient)
    machine = type("Machine", (), {"access": "agent", "id": "node1"})()
    client = transport.for_machine(machine, tmp_path / "config.json")
    assert isinstance(client, AgentClient)
    assert client.path == tmp_path / "config.json"
