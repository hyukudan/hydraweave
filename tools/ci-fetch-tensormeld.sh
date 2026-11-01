#!/usr/bin/env bash
# CI only: fetch the pinned TensorMeld submodule. TensorMeld is a public repository, so
# the fetch needs no token; the other submodules (engine, trellis) are never fetched in CI.
set -euo pipefail

export GIT_TERMINAL_PROMPT=0
git submodule update --init --depth 1 third_party/tensormeld
