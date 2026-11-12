from .base import Backend

BACKEND = Backend("ds41", ("ds41-pair",), ("pair",), ("hip",),
                  {"json_event": "READY"}, "admin-drain",
                  frozenset({"chat", "completions", "stream", "usage"}),
                  {"binary": "strix-deepseek41-serve", "targets": ("strix-deepseek41-serve",), "topology": "pair", "ready": "ds41-json", "health_path": "/health", "drain": "admin-drain", "bind": "lan", "key_file": True, "trellis": "ds41", "label": "hl-ds41", "layout_required": "1", "capabilities": ("http-serve", "key-file", "metrics", "api-status", "admin-drain", "import-state", "import-state-parked")},
                  ("deepseek-v4.1-flash", "ds41_model"), ("ds41-rtx-prefill", "ds41-rtx-front", "ds41-rtx-hot", "ds41-node"))
