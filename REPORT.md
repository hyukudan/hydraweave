# Release installer and ROCm discovery

Files changed:
- .github/workflows/release.yml; .github/workflows/ci.yml
- install.sh; tools/make_release.py; versions.lock
- node/hydraweave_rocm.py; node/build-node.sh; node/hydraweave_install.py; node/hydraweave-node
- tests/test_install_sh.py; tests/test_rocm_detection.py; tests/test_release.py
- tests/test_node_install.py; tests/test_engines.py
- README.md; docs/install.md; REPORT.md

Design decisions:
- Only matching v* tag pushes publish; workflow_dispatch always builds without publishing.
- Reuse source archives; add python -m build wheel/sdist and combined asset SHA256SUMS.
- Optional, non-blocking gfx1151 Qwen engine build uses the ROCm container and an opt-in repository variable.
- POSIX installer uses Python stdlib for metadata, verification, venvs and quoted shims; no automatic root steps.
- Offline installs verify local wheels and use pip without indexes or source distributions.
- Shared ROCm detector selects ROCM_PATH, standard SDK, then numerically newest versioned SDK.
- Record detected root/runtime libraries; choose the compiler dynamically; align controller floor to Python 3.10.

Tests added: installer flags/offline shims/hash rejection, release publication guard/checksums,
ROCm selection/runtime paths, missing-SDK failure and build manifest/compiler integration.
Targeted run: 155 passed, 16 skipped (Windows; POSIX-only cases skipped).
Full suite: python -m pytest -q -x; 91 passed, 1 skipped, 1 error; stopped on pytest temp-directory permission denial.
Tests used the exact pinned TensorMeld source in an ignored local test venv; submodules were unchanged.
Shell syntax and embedded Python syntax passed; ShellCheck was unavailable.

Docs changed: Linux one-liner first, Windows second, release-by-tag/no-release-yet status,
offline/prefix/node options, Python floors, ROCm discovery and optional engine limitations. No Spanish twins exist.
Open issues: full-suite completion needs a writable pytest temp directory; POSIX install cases need Linux execution.
Release/container builds were not executed; local TensorMeld routing still needs pinned source; engine activation is manual.
No commits, pushes, tags, workflow triggers or submodule changes were made.

Commit subject: Add tag-driven releases, verified POSIX installer and shared ROCm discovery
Docs: Explain Linux-first installation, release availability and ROCm selection.
