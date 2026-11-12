from .base import Backend

BACKEND = Backend("minimax", ("minimax-pair",), ("pair",), ("hip",),
                  {"stream": "stdout", "regex": "READY rank 0"}, "leader-first",
                  frozenset({"chat", "completions", "stream"}),
                  {"binary": "strix-minimax-serve", "targets": ("strix-minimax-serve",), "topology": "pair", "ready": "minimax-stdout", "health_path": "/health", "drain": "leader-first", "bind": "loopback", "key_file": False, "trellis": "minimax", "label": "hl-mm", "layout_required": "1", "capabilities": ("http-serve", "metrics")},
                  ("minimax-m3.1-flash", "mm_model"), ("minimax-rtx-prefill",))
