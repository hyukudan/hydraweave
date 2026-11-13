"""LAN authentication and bootstrap integrity, with in-memory HTTP and no network or hardware."""
from email.message import Message
from email.parser import BytesParser
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest import mock

import unittest

try:
    import pytest
except ImportError:  # the unittest job; the pytest job runs this module
    raise unittest.SkipTest("needs pytest")

from hydraweave import bootstrap, cli, node_agent, secrets
from hydraweave.web import server as portal


ACCESS = "a" * 64
AGENT_ACCESS = "b" * 43
CSRF = "c" * 43


def make_server(*, listen="0.0.0.0", no_auth=False):
    def init(server, address, handler):
        server.server_port = address[1]

    api = SimpleNamespace(read=mock.Mock(return_value={"ok": True}),
                          mutate=mock.Mock(return_value={"job": "test"}))
    with mock.patch.object(portal.ThreadingHTTPServer, "__init__", init), \
            mock.patch.object(secrets, "ensure", return_value=ACCESS) as ensure:
        server = portal.create_server(18700, api=api, listen=listen, no_auth=no_auth)
    server.csrf_token = CSRF
    if server.access_token is not None:
        # The portal reads the stored secret for every decision; this fixture server has a fixed one and never
        # touches the host's store (the live-store tests below use the real read).
        server.current_access = lambda: ACCESS
    return server, ensure


def request(server, method="GET", path="/", *, headers=None, body=None, handler=portal.Handler):
    """Run the actual HTTP handler against byte streams instead of opening a socket."""
    instance = handler.__new__(handler)
    instance.server, instance.command, instance.path = server, method, path
    instance.request_version, instance.requestline = "HTTP/1.1", f"{method} {path} HTTP/1.1"
    instance.connection = mock.Mock()
    instance.headers = Message()
    raw = json.dumps({} if body is None else body).encode()
    for name, value in {"Host": "127.0.0.1:18700", "Content-Type": "application/json",
                        "Content-Length": str(len(raw)), **(headers or {})}.items():
        instance.headers[name] = value
    instance.rfile, instance.wfile = io.BytesIO(raw), io.BytesIO()
    getattr(instance, "do_" + method)()
    head, _, payload = instance.wfile.getvalue().partition(b"\r\n\r\n")
    status, _, fields = head.partition(b"\r\n")
    return int(status.split()[1]), BytesParser().parsebytes(fields), payload


@pytest.fixture(autouse=True)
def auth_environment(monkeypatch):
    monkeypatch.delenv("HYDRAWEAVE_NO_AUTH", raising=False)


@pytest.fixture
def package_cache(tmp_path, monkeypatch):
    source = tmp_path / "source.py"
    source.write_text("original", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "_sources", lambda: [(source, "hydraweave/source.py")])
    monkeypatch.setattr(bootstrap, "_cache_key", ())
    monkeypatch.setattr(bootstrap, "_cache_package", b"")
    monkeypatch.setattr(bootstrap, "_cache_sha256", "")
    return source


def test_package_hash_matches_bytes_and_cache_invalidates_by_mtime(package_cache):
    with mock.patch.object(bootstrap.tarfile, "open", wraps=bootstrap.tarfile.open) as build:
        data, digest = bootstrap.package()
        assert digest == hashlib.sha256(data).hexdigest()
        assert bootstrap.source_package() is data
        assert bootstrap.package_sha256() == digest
        assert build.call_count == 1
        stat = package_cache.stat()
        os.utime(package_cache, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
        bootstrap.package()
        assert build.call_count == 2
        package_cache.write_text("changed", encoding="utf-8")
        changed, changed_digest = bootstrap.package()
        assert changed != data and changed_digest != digest
        assert changed_digest == hashlib.sha256(changed).hexdigest()


def test_bootstrap_download_is_verified_before_any_install(package_cache):
    digest = bootstrap.package_sha256()
    script = bootstrap.script("http://192.0.2.10:18700").decode()
    assert f"expected='{digest}'" in script
    assert "HYDRAWEAVE_PKG_SHA256:-" in script
    assert "using the SHA-256 embedded in this script" in script
    assert "sha256sum" in script and "shasum -a 256" in script and "hashlib.sha256" in script
    assert "hashlib.file_digest" not in script  # Python 3.10 is supported.
    assert 'mktemp -d' in script and 'trap \'rm -rf "$package_dir"\' EXIT' in script
    assert 'curl -fsSL \'http://192.0.2.10:18700/node/package.tar.gz\' -o "$package"' in script
    assert script.index('if [ "$actual" != "$expected" ]') < script.index(' -m venv ')
    assert script.index("installation aborted.") < script.index(" -m pip install ")
    assert 'file://$package' in script
    assert "pip install --upgrade --force-reinstall 'hydraweave[node] @ http" not in script
    with pytest.raises(ValueError, match="sha256"):
        bootstrap.script("http://192.0.2.10:18700", "malformed")


@pytest.mark.skipif(os.name == "nt", reason="Runs a POSIX shell with POSIX executable links")
@pytest.mark.parametrize("verifier", ["sha256sum", "shasum", "python3"])
@pytest.mark.parametrize("expected", ["match", "mismatch", "embedded"])
def test_shell_verification_success_mismatch_fallback_and_cleanup(tmp_path, verifier, expected):
    executable = sys.executable if verifier == "python3" else shutil.which(verifier)
    shell = shutil.which("sh")
    if not executable or not shell:
        pytest.skip("verification tool or POSIX shell unavailable")
    tools = tmp_path / "bin"
    tools.mkdir()
    for name in ("mktemp", "rm"):
        tool = shutil.which(name)
        if not tool:
            pytest.skip(name + " unavailable")
        (tools / name).symlink_to(tool)
    (tools / verifier).symlink_to(executable)
    curl = tools / "curl"
    curl.write_text('#!/bin/sh\nprintf %s "archive bytes" > "$4"\n', encoding="utf-8")
    curl.chmod(0o700)
    digest = hashlib.sha256(b"archive bytes").hexdigest()
    script = bootstrap.script("http://192.0.2.10:18700", digest).decode()
    # Execute the actual download/verification block; no venv, pip, service, or network calls.
    verification = script[script.index("expected=${"):script.index('mkdir -p "$root/etc"')]
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    env = {**os.environ, "PATH": str(tools), "TMPDIR": str(downloads)}
    env.pop("HYDRAWEAVE_PKG_SHA256", None)
    if expected != "embedded":
        env["HYDRAWEAVE_PKG_SHA256"] = digest if expected == "match" else "0" * 64
    result = subprocess.run([shell, "-c", "set -eu\n" + verification + "echo verified\n"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == (1 if expected == "mismatch" else 0), result.stderr
    assert ("verified" in result.stdout) == (expected != "mismatch")
    assert ("SHA-256 mismatch" in result.stderr) == (expected == "mismatch")
    assert ("using the SHA-256 embedded" in result.stderr) == (expected == "embedded")
    assert list(downloads.iterdir()) == []


def test_access_secret_is_generated_once_in_private_store(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets, "_directory", lambda: tmp_path / "secrets")
    with mock.patch.object(portal.ThreadingHTTPServer, "__init__",
                           lambda server, *_: setattr(server, "server_port", 18700)):
        first = portal.create_server(18700, api=object(), listen="0.0.0.0")
        second = portal.create_server(18700, api=object(), listen="0.0.0.0")
    assert first.access_token == second.access_token
    assert len(first.access_token) == 64
    path = tmp_path / "secrets" / "portal-access"
    assert path.is_file()
    if os.name == "nt":
        assert first.access_token.encode() not in path.read_bytes()  # DPAPI, user scope.


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_access_secret_has_mode_0600(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets, "_directory", lambda: tmp_path)
    secrets.ensure("portal-access")
    assert (tmp_path / "portal-access").stat().st_mode & 0o777 == 0o600


def test_lan_default_requires_secret_and_fails_closed_on_storage_error():
    server, ensure = make_server()
    assert server.access_token == ACCESS
    ensure.assert_called_once_with("portal-access")
    with mock.patch.object(secrets, "ensure", side_effect=OSError("unwritable")), \
            pytest.raises(OSError, match="unwritable"):
        portal.create_server(api=object(), listen="0.0.0.0")


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_lan_mutations_without_credentials_are_401(method):
    server, _ = make_server()
    status, _, _ = request(server, method, "/hydraweave/api/up")
    assert status == 401
    server.api.mutate.assert_not_called()


BROWSER = {"Origin": "http://127.0.0.1:18700", "X-Hydraweave-CSRF": CSRF}


def sign_in(server, token=ACCESS, headers=BROWSER):
    return request(server, "POST", "/hydraweave/api/login", headers=headers, body={"token": token})


def test_signed_out_page_is_the_sign_in_screen_only(package_cache):
    server, _ = make_server()
    status, _, body = request(server)
    assert status == 200 and CSRF.encode() in body
    assert b'/security.js' in body and b'name="hydraweave-login"' in body and b"Signed out" in body
    assert ACCESS.encode() not in body
    # No application shell, scripts or deployment details before sign-in.
    assert b"app.js" not in body and bootstrap.package_sha256().encode() not in body
    cookie = sign_in(server)[1]["Set-Cookie"].split(";", 1)[0]
    body = request(server, headers={"Cookie": cookie})[2]
    assert b'name="hydraweave-login"' not in body and bootstrap.package_sha256().encode() in body
    assert request(server, path="/hydraweave/api/up")[0] == 401
    assert request(server, path="/hydraweave/api/up", headers={"Cookie": cookie})[0] == 405


def test_sign_in_sets_cookie_and_cookie_mutation(package_cache):
    server, _ = make_server()
    status, headers, body = sign_in(server)
    assert status == 200 and json.loads(body) == {"signed_in": True, "required": True}
    cookie = headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie
    status, _, _ = request(server, "POST", "/hydraweave/api/up", headers={
        "Cookie": cookie.split(";", 1)[0], "Origin": server.origin, "X-Hydraweave-CSRF": CSRF})
    assert status == 202
    server.api.mutate.assert_called_once_with("up", {})
    # A cookie does not bypass the existing cross-site protections.
    assert request(server, "POST", "/hydraweave/api/up", headers={"Cookie": cookie})[0] == 403


@pytest.mark.parametrize("query", ["token=wrong", "token=", "token=" + ACCESS, "token=%C3%B1"])
def test_token_in_a_url_is_refused_and_never_sets_cookie(query):
    server, _ = make_server()
    for path in ("/?" + query, "/hydraweave/api/devices?" + query):
        status, headers, body = request(server, path=path)
        assert status == 400 and b"never accepted in a URL" in body
        assert headers["Set-Cookie"] is None
    assert request(server, "POST", "/hydraweave/api/login?" + query, headers=BROWSER,
                   body={"token": ACCESS})[0] == 404


@pytest.mark.parametrize("token,headers,expected", [
    ("wrong", BROWSER, 401), ("", BROWSER, 401), ("\u00f1" * 64, BROWSER, 401), (None, BROWSER, 400),
    (ACCESS, {}, 403), (ACCESS, {"Origin": "http://evil.example", "X-Hydraweave-CSRF": CSRF}, 403),
    (ACCESS, {"Origin": "http://127.0.0.1:18700", "X-Hydraweave-CSRF": "wrong"}, 403)])
def test_invalid_sign_in_never_sets_cookie(token, headers, expected):
    server, _ = make_server()
    status, fields, _ = sign_in(server, token, headers)
    assert status == expected
    assert fields["Set-Cookie"] is None


def live_server(tmp_path, monkeypatch, **api):
    monkeypatch.setattr(secrets, "_directory", lambda: tmp_path / "secrets")
    with mock.patch.object(portal.ThreadingHTTPServer, "__init__",
                           lambda server, *_: setattr(server, "server_port", 18700)):
        server = portal.create_server(18700, api=SimpleNamespace(read=mock.Mock(return_value={"ok": True}), **api),
                                      listen="0.0.0.0")
    server.csrf_token = CSRF
    return server


def test_rotation_between_comparison_and_cookie_never_discloses_the_new_token():
    server, _ = make_server()
    new = "d" * 64
    reads = iter([ACCESS, ACCESS])                  # the "is a token required" check, then the comparison
    server.current_access = lambda: next(reads, new)        # every later read sees the rotated secret
    status, fields, _ = sign_in(server, ACCESS)
    assert status == 200
    assert new not in str(fields) and fields["Set-Cookie"].startswith("hydraweave-access=" + ACCESS + ";")
    # The cookie the old holder got is the old token: it opens nothing after the rotation.
    cookie = fields["Set-Cookie"].split(";", 1)[0]
    assert request(server, path="/hydraweave/api/devices", headers={"Cookie": cookie})[0] == 401


@pytest.mark.skipif(os.name == "nt" or getattr(os, "geteuid", lambda: 1)() == 0,
                    reason="needs POSIX file modes and a non-root user")
def test_permission_loss_closes_the_portal_with_unchanged_content(tmp_path, monkeypatch):
    server = live_server(tmp_path, monkeypatch)
    bearer = {"Authorization": "Bearer " + server.access_token}
    assert request(server, path="/hydraweave/api/devices", headers=bearer)[0] == 200
    stored = tmp_path / "secrets" / "portal-access"
    before = stored.stat()
    stored.chmod(0)
    try:
        after = stored.stat()
        assert (after.st_mtime_ns, after.st_size, after.st_ino) == (before.st_mtime_ns, before.st_size, before.st_ino)
        assert request(server, path="/hydraweave/api/devices", headers=bearer)[0] == 401
        assert sign_in(server, bearer["Authorization"][7:])[0] == 401
    finally:
        stored.chmod(0o600)
    assert request(server, path="/hydraweave/api/devices", headers=bearer)[0] == 200


def test_unreadable_secret_closes_the_portal_on_the_next_request(tmp_path, monkeypatch):
    """The same decision on every platform: a read that fails is never answered from a remembered token."""
    server = live_server(tmp_path, monkeypatch)
    bearer = {"Authorization": "Bearer " + server.access_token}
    assert request(server, path="/hydraweave/api/devices", headers=bearer)[0] == 200
    with mock.patch.object(secrets, "get", side_effect=PermissionError("denied")):
        assert request(server, path="/hydraweave/api/devices", headers=bearer)[0] == 401
        assert request(server, "POST", "/hydraweave/api/up", headers=bearer)[0] == 401
    assert request(server, path="/hydraweave/api/devices", headers=bearer)[0] == 200


def test_rotation_invalidates_cookie_and_bearer_live(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets, "_directory", lambda: tmp_path / "secrets")
    with mock.patch.object(portal.ThreadingHTTPServer, "__init__",
                           lambda server, *_: setattr(server, "server_port", 18700)):
        server = portal.create_server(18700, api=SimpleNamespace(read=mock.Mock(return_value={"ok": True})),
                                      listen="0.0.0.0")
    server.csrf_token = CSRF
    old = server.access_token
    cookie = sign_in(server, old)[1]["Set-Cookie"].split(";", 1)[0]
    for headers in ({"Cookie": cookie}, {"Authorization": "Bearer " + old}):
        assert request(server, path="/hydraweave/api/devices", headers=headers)[0] == 200
    new = secrets.rotate("portal-access")
    assert new != old
    for headers in ({"Cookie": cookie}, {"Authorization": "Bearer " + old}):
        assert request(server, path="/hydraweave/api/devices", headers=headers)[0] == 401
    assert b'name="hydraweave-login"' in request(server, headers={"Cookie": cookie})[2]
    assert sign_in(server, old)[0] == 401
    assert request(server, path="/hydraweave/api/devices", headers={"Authorization": "Bearer " + new})[0] == 200
    # A secret that disappears closes the portal; it never falls back to open access.
    (tmp_path / "secrets" / "portal-access").unlink()
    assert request(server, path="/hydraweave/api/devices", headers={"Authorization": "Bearer " + new})[0] == 401


@pytest.mark.parametrize("headers", [{"Cookie": "hydraweave-access=wrong"},
                                      {"Authorization": "Bearer wrong"},
                                      {"Authorization": "Bearer " + AGENT_ACCESS}])
def test_wrong_credentials_and_agent_token_cannot_mutate_portal(headers):
    server, _ = make_server()
    assert request(server, "POST", "/hydraweave/api/up", headers=headers)[0] == 401


def test_bearer_mutation_works_without_browser_csrf_headers():
    server, _ = make_server()
    assert request(server, "POST", "/hydraweave/api/up",
                   headers={"Authorization": "Bearer " + ACCESS})[0] == 202


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH"])
def test_authenticated_unsupported_methods_still_return_405(method):
    server, _ = make_server()
    assert request(server, method, "/hydraweave/api/up",
                   headers={"Authorization": "Bearer " + ACCESS})[0] == 405


@pytest.mark.parametrize("listen,no_auth,environment", [("0.0.0.0", True, "0"),
                                                         ("0.0.0.0", False, "1"),
                                                         ("127.0.0.1", False, "0"),
                                                         ("::1", False, "0")])
def test_no_auth_and_loopback_keep_existing_behavior(listen, no_auth, environment, monkeypatch):
    monkeypatch.setenv("HYDRAWEAVE_NO_AUTH", environment)
    server, ensure = make_server(listen=listen, no_auth=no_auth)
    ensure.assert_not_called()
    assert server.access_token is None
    assert request(server, "POST", "/hydraweave/api/up")[0] == 403  # Still needs CSRF.
    assert request(server, "POST", "/hydraweave/api/up", headers={
        "Origin": "http://127.0.0.1:18700", "X-Hydraweave-CSRF": CSRF})[0] == 202


def test_no_auth_lan_has_visible_warning_and_loopback_does_not(package_cache):
    lan, _ = make_server(no_auth=True)
    assert b"WARNING: LAN access without authentication" in request(lan)[2]
    local, _ = make_server(listen="127.0.0.1", no_auth=True)
    assert b"WARNING: LAN access without authentication" not in request(local)[2]


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_bootstrap_endpoints_need_the_portal_token(method, package_cache):
    server, _ = make_server()
    data, digest = bootstrap.package()
    for path, expected in [("/node/package.tar.gz", data),
                           ("/node/bootstrap.sh", bootstrap.script(server.origin, digest))]:
        assert request(server, method, path)[0] == 401
        status, headers, body = request(server, method, path, headers={"Authorization": "Bearer " + ACCESS})
        assert status == 200
        assert headers["Set-Cookie"] is None
        assert body == (expected if method == "GET" else b"")
    script = bootstrap.script(server.origin, digest).decode()
    assert "HYDRAWEAVE_PORTAL_TOKEN" in script and "curl -fsSL -K -" in script and ACCESS not in script


def test_agent_pair_and_call_keep_their_own_authentication():
    agent = SimpleNamespace(server_port=18700, token=AGENT_ACCESS,
                            pair=mock.Mock(return_value={"token": AGENT_ACCESS}),
                            invoke=mock.Mock(return_value={"ok": True}))
    with mock.patch.object(node_agent, "load_tool", return_value=SimpleNamespace(decode_json=json.loads)):
        status, _, body = request(agent, "POST", "/pair", body={"code": "123456"}, handler=node_agent.AgentHandler)
        assert status == 200 and json.loads(body)["token"] == AGENT_ACCESS
        agent.pair.assert_called_once_with("123456")
        for token, expected in [(None, 403), (ACCESS, 403), (AGENT_ACCESS, 200)]:
            status, _, _ = request(agent, "POST", "/call", handler=node_agent.AgentHandler,
                                   body={"verb": "status", "payload": {}},
                                   headers={"Authorization": "Bearer " + token} if token else {})
            assert status == expected
        agent.invoke.assert_called_once_with("status", {})


def test_cli_passes_no_auth():
    with mock.patch.object(portal, "serve") as serve:
        assert cli.main(["ui", "--listen", "0.0.0.0", "--no-auth", "--no-browser"]) == 0
    assert serve.call_args.kwargs["no_auth"] is True


def test_console_prints_token_and_browser_url_has_no_token(capsys):
    fake = mock.MagicMock()
    fake.__enter__.return_value = fake
    fake.lan, fake.no_auth, fake.access_token = True, False, ACCESS
    fake.origin, fake.listen_address, fake.server_port = "http://192.0.2.10:18700", "192.0.2.10", 18700
    fake.serve_forever.side_effect = KeyboardInterrupt
    with mock.patch.object(portal, "create_server", return_value=fake), \
            mock.patch.object(portal, "start_telemetry"), mock.patch("hydraweave.nodepoll.start"), \
            mock.patch("hydraweave.nodegate.portal"), mock.patch("hydraweave.onboarding.of"), \
            mock.patch.object(portal.webbrowser, "open") as browse:
        portal.serve(listen="192.0.2.10")
    captured = capsys.readouterr()
    assert "access token (enter it on the sign-in screen): " + ACCESS in captured.out
    assert "?token=" not in captured.out and "without authentication" not in captured.err
    browse.assert_called_once_with(fake.origin + "/")


def test_ui_prompts_for_login_in_each_mutation_transport():
    root = Path(portal.STATIC_DIR)
    for name in ("app.js", "recipes.js", "pages/planner.js"):
        source = (root / name).read_text(encoding="utf-8")
        assert "response.status === 401" in source and "window.HydraweaveLogin?.()" in source
    login = (root / "security.js").read_text(encoding="utf-8")
    assert 'name="token" type="password"' in login and 'method="post"' in login
    assert 'method="get"' not in login and "?token" not in login and "location.reload()" in login
    control = (root / "pages/control-center.js").read_text(encoding="utf-8")
    assert "HYDRAWEAVE_PKG_SHA256=" in control and "Package SHA-256:" in control
