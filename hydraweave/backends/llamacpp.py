"""Argument builder for a prebuilt llama-server (not yet wired into managed profile launches)."""
from .base import Backend


def _text(value, name):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"llamacpp {name} must be nonempty text without NUL")
    return value


def _integer(value, name, minimum, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        suffix = f"..{maximum}" if maximum is not None else " or greater"
        raise ValueError(f"llamacpp {name} must be an integer {minimum}{suffix}")
    return value


class LlamaCpp(Backend):
    def processes(self, profile, members):
        model = _text(profile.get("model_path") or profile.get("model"), "model_path")
        ctx = _integer(profile.get("context", 4096), "context", 1)
        ngl = _integer(profile.get("n_gpu_layers", 0), "n_gpu_layers", -1)
        host = _text(profile.get("host", "127.0.0.1"), "host")
        port = _integer(profile.get("port", 8080), "port", 1, 65535)
        binary = _text(profile.get("binary", "llama-server"), "binary")
        extra = profile.get("extra_args", ())
        if not isinstance(extra, (list, tuple)) or not all(isinstance(arg, str) and arg and "\x00" not in arg
                                                         for arg in extra):
            raise ValueError("llamacpp extra_args must be a list of argument strings")
        argv = [binary, "-m", model, "--host", host, "--port", str(port), "-c", str(ctx), "-ngl", str(ngl)]
        argv.extend(extra)
        return [{"name": str(member), "argv": argv.copy(), "env": {}} for member in members]

BACKEND = LlamaCpp("llamacpp", ("llamacpp",), ("replicas",), ("cuda", "vulkan", "cpu", "hip"),
                   {"http": "/health"}, "sigterm",
                   frozenset({"chat", "completions", "stream", "embeddings", "usage"}),
                   {"binary": "llama-server", "targets": ("llama-server",), "topology": "replicas", "ready": "http-health", "health_path": "/health", "drain": "sigterm", "bind": "loopback", "key_file": False, "trellis": None, "label": "hl-llama", "layout_required": None, "capabilities": ("http-serve",)},
                   ("llamacpp-generic", "model"))
