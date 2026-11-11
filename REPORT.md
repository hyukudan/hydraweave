# Security PR report

Branch: hw/p1-security. Changes remain uncommitted; no tags, pushes, or submodule changes.

Files changed:
- hydraweave/bootstrap.py: cached package bytes and SHA-256, verified temporary download before pip.
- hydraweave/web/server.py: LAN access control, URL login/cookie flow, public bootstrap routes.
- hydraweave/cli.py and hydraweave/bind.py: --no-auth option and updated bind guidance.
- hydraweave/web/static/security.js: small login form displayed after mutation 401 responses.
- hydraweave/web/static/app.js, recipes.js, pages/planner.js: login prompts on 401.
- hydraweave/web/static/pages/control-center.js: pinned bootstrap command and visible SHA-256.
- tests/test_portal_security.py; tests/test_bind.py; tests/test_autodiscover.py.
- README.md; docs/install.md; docs/node-discovery.md; REPORT.md.

Design decisions:
- Cache the archive and digest together by source file list, mtimes, and sizes under a lock.
- Reuse secrets.ensure("portal-access"): persistent 0600 POSIX file; private ACL and user DPAPI on Windows.
- LAN writes require cookie or bearer; cookie requests retain Origin/CSRF checks. Bearer requests use explicit credentials.
- Login redirects remove the token query and set an HttpOnly, SameSite=Strict session cookie.
- Loopback and explicit --no-auth/HYDRAWEAVE_NO_AUTH=1 preserve existing access rules.
- Bootstrap/package GET/HEAD remain public; worker POST /pair and /call retain pairing-code/agent-bearer auth.

Validation:
- Focused: test_portal_security.py, test_bind.py, test_autodiscover.py, test_web_security.py: 99 passed, 11 skipped.
- Added in-memory HTTP tests, hash/cache checks, token persistence/permissions, cookie/bearer/401 flows, and agent isolation.
- Added POSIX-only execution tests for all three checksum paths, mismatch abort, fallback note, and cleanup.
- JavaScript syntax checks passed for all five changed scripts.
- Full suite (-q -x): 1 failed, 70 passed in 30.85s; stopped on missing module tensormeld.
- Failure: tests/test_assets.py::AssetTests::test_pair_cross_rank_mismatch_and_one_unknown_rank.
- Sandbox temp restriction handled with TEMP/TMP and pytest basetemp under .pytest_cache/security-tmp.

Docs: English README/install/node-discovery updated; no Spanish twins exist for these documents.
Open issues: Full suite cannot pass until the missing tensormeld import is resolved; Linux-only execution tests were skipped on Windows.
HTTP transport still requires a trusted LAN or secure tunnel for scripts and credentials.

Commit subject: Protect LAN portal mutations and verify bootstrap package SHA-256
