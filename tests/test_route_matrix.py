"""Route matrix: every registered portal route is denied without the access token unless it is on the explicit
public allow-list, and allowed with it. In-memory HTTP, no network or hardware.

The gate is default-deny, so a route added later is closed without any change here; widening the public
surface means editing PUBLIC_GET / PUBLIC_POST, which this module pins."""
import inspect
from unittest import mock

import unittest

try:
    import pytest
except ImportError:  # the unittest job; the pytest job runs this module
    raise unittest.SkipTest("needs pytest")

from hydraweave.compat import LEGACY_NAME
from hydraweave.web import api as web_api, features as feature_registry, recipes as recipe_web, server as portal
from tests.test_portal_security import ACCESS, CSRF, make_server, request

API = "/hydraweave/api/"
BEARER = {"Authorization": "Bearer " + ACCESS}
BROWSER = {"Origin": "http://127.0.0.1:18700", "X-Hydraweave-CSRF": CSRF}       # a same-origin page, signed out
EXPECTED_PUBLIC_GET = {"/api/health", "/", "/index.html", "/hydraweave/", "/hydraweave/index.html",
                       "/app.css", "/hydraweave/app.css", "/security.js", "/favicon.ico"}
EXPECTED_PUBLIC_POST = {"/hydraweave/api/login"}


def post_names():
    registry = feature_registry.registry()
    return sorted(set(web_api.MUTATIONS) | set(recipe_web.POSTS) | set(registry.posts) | set(registry.jobs)
                  | {"jobs/renew", "login"})


def get_paths():
    """Every path the GET/HEAD dispatch can serve, from the same tables the server routes with."""
    registry = feature_registry.registry()
    paths = {"/api/health", "/metrics", "/node/bootstrap.sh", "/node/package.tar.gz", "/favicon.ico", "/sw.js"}
    paths |= set(portal.STATIC_FILES)
    for directory in portal.ASSET_DIRS:
        for name in portal.manifest(portal.STATIC_DIR, directory):
            paths |= {f"/{directory}/{name}", f"/hydraweave/{directory}/{name}"}
    names = set(web_api.SCREENS) | {"jobs", "jobs/some-job", "stream", "recipes/some-recipe"}
    names |= set(recipe_web.READS) | set(registry.gets) | {prefix + "some-id" for prefix in registry.prefixes}
    names |= set(post_names())                       # a GET of an action must not leak either (405 when signed in)
    paths |= {API + name for name in names}
    paths |= {"/hydraweave/api/not-a-route", "/not-a-route", "/hydraweave/pages/not-a-file.js"}
    # The legacy alias reaches the same handlers.
    paths |= {"/" + LEGACY_NAME + path[len("/hydraweave"):] for path in paths if path.startswith("/hydraweave/")}
    return sorted(paths)


@pytest.fixture
def server():
    server, _ = make_server()
    server.api = mock.MagicMock()
    server.api.read.return_value = {"ok": True}
    server.api.mutate.return_value = {"job": "test"}
    for name in ("recipe_get", "recipe_post", "renew_jobs", "feature_post"):
        getattr(server.api, name).return_value = (200, {"ok": True})

    def stream(handler, head=False):
        handler._send(200, b"", "text/event-stream", head=head)

    with mock.patch("hydraweave.web.features.integrations.serve_stream", side_effect=stream) as served, \
            mock.patch("hydraweave.metrics_export.collect", return_value=b"") as collected, \
            mock.patch.object(portal, "_page_seen"):
        server.served_stream, server.collected = served, collected
        yield server


def test_public_surface_is_exactly_the_documented_allow_list():
    assert set(portal.PUBLIC_GET) == EXPECTED_PUBLIC_GET
    assert set(portal.PUBLIC_POST) == EXPECTED_PUBLIC_POST
    assert len(get_paths()) > 150 and len(post_names()) > 60        # the walk really sees the registries


def test_one_gate_precedes_every_handler():
    get = inspect.getsource(portal.Handler._get).split("self._gate(PUBLIC_GET)")
    assert len(get) == 2 and get[0].count("parsed.path") == 1 and '"/api/health"' in get[0]
    post = inspect.getsource(portal.Handler.do_POST).split("self._gate(PUBLIC_POST)")
    assert len(post) == 2 and "self._send(" not in post[0] and "self.server.api" not in post[0]
    assert "self._gate(frozenset())" in inspect.getsource(portal.Handler._method)


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_every_read_route_needs_the_token_except_the_allow_list(server, method):
    for path in get_paths():
        status, _, body = request(server, method, path)
        if portal.api_path(path) in EXPECTED_PUBLIC_GET:        # a legacy alias is the same route
            assert status in (200, 204), (method, path, status)
        else:
            assert status == 401, (method, path, status)
            assert method == "HEAD" or b"sign-in required" in body, path
    # Nothing behind the gate ran for a signed-out visitor.
    assert not server.api.method_calls and not server.served_stream.called and not server.collected.called
    for path in get_paths():
        for headers in (BEARER, {"Cookie": "hydraweave-access=" + ACCESS}):
            status = request(server, method, path, headers=headers)[0]
            assert status not in (401, 403), (method, path, status)
    assert server.api.read.called and server.served_stream.called and server.collected.called


def test_signed_out_index_serves_no_application_shell(server):
    for path in ("/", "/index.html", "/hydraweave/", "/hydraweave/index.html", "/" + LEGACY_NAME + "/"):
        body = request(server, path=path)[2]
        assert b'name="hydraweave-login"' in body and b"app.js" not in body and b"/pages/" not in body, path


def test_every_action_route_needs_the_token_except_sign_in(server):
    for name in post_names():
        for prefix in (API, "/" + LEGACY_NAME + "/api/"):
            status = request(server, "POST", prefix + name, headers=BROWSER, body={"token": "wrong"})[0]
            assert status == 401, (prefix + name, status)       # sign-in itself: a wrong token
    assert request(server, "POST", "/hydraweave/api/not-a-route", headers=BROWSER)[0] == 401
    assert not server.api.method_calls
    for name in post_names():
        body = {"token": ACCESS} if name == "login" else {}
        for headers in (BEARER, {**BROWSER, "Cookie": "hydraweave-access=" + ACCESS}):
            status = request(server, "POST", API + name, headers=headers, body=body)[0]
            assert status not in (401, 403, 404), (name, status)
    assert request(server, "POST", "/hydraweave/api/not-a-route", headers=BEARER)[0] == 404


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH", "OPTIONS", "TRACE"])
def test_other_methods_reveal_nothing_signed_out(server, method):
    for path in ("/", "/api/health", "/hydraweave/api/devices", "/hydraweave/api/login"):
        assert request(server, method, path)[0] == 401
        assert request(server, method, path, headers=BEARER)[0] == 405
