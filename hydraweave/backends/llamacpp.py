"""Generic prebuilt llama-server backend."""
from .base import Backend

class LlamaCpp(Backend):
    def processes(self, profile, members):
        model = profile.get("model_path") or profile.get("model")
        if not isinstance(model, str) or not model:
            raise ValueError("llamacpp profile requires model_path")
        ctx = int(profile.get("context", 4096))
        ngl = int(profile.get("n_gpu_layers", 0))
        host = str(profile.get("host", "127.0.0.1"))
        port = int(profile.get("port", 8080))
        binary = str(profile.get("binary", "llama-server"))
        argv = [binary, "-m", model, "--host", host, "--port", str(port), "-c", str(ctx), "-ngl", str(ngl)]
        argv.extend(profile.get("extra_args", ()))
        return [{"name": str(member), "argv": argv.copy(), "env": {}} for member in members]

BACKEND = LlamaCpp("llamacpp", ("llamacpp",), ("replicas",), ("cuda", "vulkan", "cpu", "hip"),
                   {"http": "/health"}, "sigterm",
                   frozenset({"chat", "completions", "stream", "embeddings", "usage"}),
                   {"binary": "llama-server", "targets": (), "topology": "replicas", "ready": "http-health", "health_path": "/health", "drain": "sigterm", "bind": "loopback", "key_file": False, "trellis": None, "label": "hl-llama", "layout_required": None, "capabilities": ("http-serve",)},
                   ("llamacpp-generic", "model"))
