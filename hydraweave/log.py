"""Logging setup shared by the command line, portal, and agent."""
from __future__ import annotations

import json as json_module
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


class _JsonFormatter(logging.Formatter):
    def format(self, record):
        from .redaction import redact_text
        return json_module.dumps({"time": self.formatTime(record), "level": record.levelname,
                                  "logger": record.name, "message": redact_text(record.getMessage())})


def setup(level="INFO", json=False, file=None):
    root = logging.getLogger("hydraweave")
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    formatter = _JsonFormatter() if json else _RedactingFormatter()
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)
    if file:
        path = Path(file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(formatter)
        root.addHandler(handler)
    return root


class _RedactingFormatter(logging.Formatter):
    def format(self, record):
        from .redaction import redact_text
        message = redact_text(record.getMessage())
        return f"{self.formatTime(record)} {record.levelname} {record.name}: {message}"
