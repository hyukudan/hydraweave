import json
import tempfile

from hydraweave import config, engines
from hydraweave.backends import get, model_registry, registry
from hydraweave.placement import ENGINES


def test_backend_registry_and_legacy_aliases():
    rows = registry()
    assert {"qwen", "ds41", "minimax", "llamacpp"} <= set(rows)
    assert engines.FAMILIES["qwen-replicas"].binary == "strix-hip-decode-token"
    assert engines.FAMILIES["ds41-pair"].ready == "ds41-json"
    assert engines.FAMILIES["minimax-pair"].capabilities == ("http-serve", "metrics")
    assert ENGINES == ("desc-v1", "ds41-node", "ds41-rtx-front", "ds41-rtx-hot", "ds41-rtx-prefill",
                       "minimax-rtx-prefill", "qwen-rtx-prefill")
    assert get("qwen-replicas").name == "qwen"


def test_model_registry_and_user_preset_discovery(monkeypatch):
    from hydraweave import secrets
    with tempfile.TemporaryDirectory() as temporary:
        tmp_path = __import__("pathlib").Path(temporary)
        models = tmp_path / "models"
        models.mkdir()
        (models / "local.json").write_text(json.dumps({"id": "local", "backend": "llamacpp", "files": [{"name": "a.gguf", "bytes": 12}]}))
        assert model_registry(user_dir=models)["local"]["files"][0]["bytes"] == 12
        state = tmp_path / "state"
        monkeypatch.setattr(secrets, "runtime_dir", lambda: state)
        custom = state / "presets"
        custom.mkdir(parents=True)
        (custom / "private.json").write_text('{"name":"private"}')
        assert "private" in config.preset_names()


def test_llamacpp_process_arguments():
    backend = get("llamacpp")
    rows = backend.processes({"model_path": "model.gguf", "context": 8192, "n_gpu_layers": 42,
                              "host": "127.0.0.1", "port": 8090, "extra_args": ["--jinja"]}, ["node1"])
    assert rows[0]["argv"] == ["llama-server", "-m", "model.gguf", "--host", "127.0.0.1", "--port", "8090",
                               "-c", "8192", "-ngl", "42", "--jinja"]
