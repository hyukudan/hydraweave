from .base import Backend

BACKEND = Backend("qwen", ("qwen-replicas",), ("replicas",), ("hip",),
                  {"stream": "stderr", "regex": "READY"}, "sigterm",
                  frozenset({"chat", "completions", "stream", "usage"}),
                  {"binary": "strix-hip-decode-token", "targets": ("strix-hip-decode-token",), "topology": "replicas", "ready": "qwen-stderr", "health_path": "/health", "drain": "sigterm", "bind": "lan", "key_file": True, "trellis": None, "label": "hl-qwen", "layout_required": None, "capabilities": ("http-serve", "key-file", "metrics", "api-status", "service-unit")},
                  ("qwen3.8-flash-next", "model"), ("qwen-rtx-prefill",))
