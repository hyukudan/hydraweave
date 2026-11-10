#!/usr/bin/env python3
"""Local-only ssh stand-in; never opens a network connection."""
import os
from pathlib import Path
import shlex
import sys

target = sys.argv[sys.argv.index("--") + 1]
if target != "halo-2":
    raise SystemExit(2)
boot = Path(__file__).resolve().parent / "peer-boot" / "hydraweave-node"
words = shlex.split(sys.argv[-1])
# The rename-compatible command chooses the current or legacy tool with if/else.
# Read the verb passed to those tools, not the shell's final `fi` token.
tools = {"$HOME/.hydraweave-boot/hydraweave-node", "$HOME/.hyperloom-boot/hyperloom-node"}
verbs = [words[i + 1].removesuffix(";") for i, word in enumerate(words[:-1]) if word in tools]
if not verbs or len(set(verbs)) != 1:
    raise SystemExit("expected one node verb in the peer command")
verb = verbs[0]
os.execv(sys.executable, [sys.executable, str(boot), verb])
