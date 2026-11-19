"""Real HTTP and concurrent-catalog regressions for the model front door (no engine/GPU)."""
import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import io
import json
import os
from pathlib import Path
import socket
import threading
import tempfile
import unittest
from unittest.mock import patch

from hydraweave import frontdoor
from hydraweave.cliext import front


@contextmanager
def serving(server):
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


class CatalogConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.upstreams = [frontdoor.Upstream(name, "http://127.0.0.1:1") for name in ("a", "b")]

    def test_probes_run_concurrently_without_serial_timeout_budget(self):
        barrier = threading.Barrier(2, timeout=1)

        def fetch(upstream, timeout):
            barrier.wait()
            return [{"id": upstream.name}]

        table = frontdoor.ModelTable(self.upstreams, fetch=fetch)
        self.assertEqual([row["id"] for row in table.listing()["data"]], ["a", "b"])

    def test_warm_routes_remain_readable_during_one_inflight_refresh(self):
        now, entered, release, read_done = [0.0], threading.Event(), threading.Event(), threading.Event()
        calls, picked = [], []

        def fetch(upstream, timeout):
            calls.append(upstream.name)
            if now[0]:
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("test refresh was not released")
            return [{"id": "model"}]

        table = frontdoor.ModelTable(self.upstreams[:1], fetch=fetch, clock=lambda: now[0])
        table.listing()
        now[0] = 20.0
        refresh = threading.Thread(target=table.refresh, daemon=True)
        reader = threading.Thread(target=lambda: (picked.append(table.pick("model")), read_done.set()), daemon=True)
        refresh.start()
        try:
            self.assertTrue(entered.wait(1))
            reader.start()
            self.assertTrue(read_done.wait(.5), "a slow catalog probe blocked an already known route")
            self.assertEqual(picked[0].name, "a")
            self.assertEqual(calls, ["a", "a"], "readers must not start duplicate refreshes")
        finally:
            release.set()
            refresh.join(3)
            if reader.ident is not None:
                reader.join(3)

    def test_refresh_preserves_replica_turn_when_membership_is_unchanged(self):
        now = [0.0]
        table = frontdoor.ModelTable(self.upstreams, clock=lambda: now[0],
                                     fetch=lambda *_: [{"id": "shared"}])
        self.assertEqual(table.pick("shared").name, "a")
        now[0] = 20.0
        self.assertEqual(table.pick("shared").name, "b")

    def test_duplicate_model_rows_do_not_give_one_replica_extra_turns(self):
        table = frontdoor.ModelTable(self.upstreams, fetch=lambda upstream, _: [
            {"id": "shared"}] * (2 if upstream.name == "a" else 1))
        self.assertEqual([table.pick("shared").name for _ in range(4)], ["a", "b", "a", "b"])


class Engine(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        data = json.dumps({"object": "list", "data": [{"id": "org/model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.seen.append((self.path, dict(self.headers), body))
        data = b'hello'
        self.send_response(200)
        self.send_header("Content-Length", str(len(data) + (7 if "truncate" in self.path else 0)))
        self.send_header("Connection", "close, X-Upstream-Hop")
        self.send_header("X-Upstream-Hop", "private-hop")
        self.send_header("X-End-To-End", "preserve")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True


class FrontFramingTests(unittest.TestCase):
    def setUp(self):
        self.contexts = []
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Engine)
        upstream.seen = []
        self.upstream = self.enter(serving(upstream))
        self.front = self.enter(serving(frontdoor.make_server([
            frontdoor.Upstream("engine", f"http://127.0.0.1:{upstream.server_port}", key="upstream-key")],
            port=0, token="front-key", ttl=60)))
        self.body = b'{"model":"org/model"}'

    def enter(self, context):
        value = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return value

    def connection(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.front.server_port, timeout=3)
        self.addCleanup(conn.close)
        return conn

    def post(self, path="/v1/chat/completions", headers=None):
        conn = self.connection()
        conn.request("POST", path, body=self.body,
                     headers={"Authorization": "Bearer front-key", **(headers or {})})
        return conn.getresponse()

    def test_request_and_response_connection_options_stay_on_their_hop(self):
        response = self.post(headers={"Connection": "X-Client-Hop", "X-Client-Hop": "private", "X-User": "keep"})
        self.assertEqual(response.read(), b"hello")
        self.assertIsNone(response.getheader("X-Upstream-Hop"))
        self.assertEqual(response.getheader("X-End-To-End"), "preserve")
        sent = {k.lower(): v for k, v in self.upstream.seen[-1][1].items()}
        self.assertNotIn("x-client-hop", sent)
        self.assertEqual(sent["x-user"], "keep")
        self.assertEqual(sent["authorization"], "Bearer upstream-key")

    def test_post_query_is_preserved(self):
        response = self.post("/v1/chat/completions?option=value")
        self.assertEqual(response.read(), b"hello")
        self.assertEqual(self.upstream.seen[-1][0], "/v1/chat/completions?option=value")

    def test_truncated_upstream_is_not_reframed_as_a_complete_response(self):
        response = self.post("/v1/truncate")
        with self.assertRaises(http.client.IncompleteRead):
            response.read()

    def test_percent_encoded_model_id_can_be_retrieved(self):
        conn = self.connection()
        conn.request("GET", "/v1/models/org%2Fmodel", headers={"Authorization": "Bearer front-key"})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read())["id"], "org/model")

    def test_ambiguous_framing_is_rejected_without_forwarding(self):
        for headers in ((('Content-Length', '21'), ('Content-Length', '21')),
                        (('Content-Length', '21'), ('Transfer-Encoding', 'chunked'))):
            with self.subTest(headers=headers):
                conn = self.connection()
                conn.putrequest("POST", "/v1/chat/completions")
                conn.putheader("Authorization", "Bearer front-key")
                for key, value in headers:
                    conn.putheader(key, str(len(self.body)) if key == "Content-Length" else value)
                conn.endheaders(self.body)
                response = conn.getresponse()
                self.assertEqual(response.status, 400)
                self.assertEqual(response.getheader("Connection"), "close")
                response.read()
                self.assertEqual(self.upstream.seen, [])

    def test_very_large_decimal_length_is_rejected_without_integer_conversion_failure(self):
        conn = self.connection()
        conn.putrequest("POST", "/v1/chat/completions")
        conn.putheader("Authorization", "Bearer front-key")
        conn.putheader("Content-Length", "9" * 5000)
        conn.endheaders()
        response = conn.getresponse()
        self.assertEqual(response.status, 413)
        response.read()
        self.assertEqual(self.upstream.seen, [])

    def test_duplicate_credentials_are_rejected_and_body_connection_is_closed(self):
        conn = self.connection()
        conn.putrequest("POST", "/v1/chat/completions")
        conn.putheader("Authorization", "Bearer front-key")
        conn.putheader("Authorization", "Bearer different")
        conn.putheader("Content-Length", str(len(self.body)))
        conn.endheaders(self.body)
        response = conn.getresponse()
        self.assertEqual(response.status, 401)
        self.assertEqual(response.getheader("Connection"), "close")
        response.read()
        self.assertEqual(self.upstream.seen, [])

    def test_ipv6_loopback_when_available(self):
        try:
            with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
                probe.bind(("::1", 0))
        except OSError:
            self.skipTest("IPv6 loopback unavailable")
        server = self.enter(serving(frontdoor.make_server([frontdoor.Upstream("a", "http://127.0.0.1:1")],
                                                        host="::1", port=0)))
        conn = http.client.HTTPConnection("::1", server.server_port, timeout=3)
        self.addCleanup(conn.close)
        conn.request("GET", "/health")
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        response.read()


class FrontCredentialTests(unittest.TestCase):
    def args(self, *values):
        parser = argparse.ArgumentParser()
        front.add(parser.add_subparsers())
        return parser.parse_args(["front", "models", "--upstream", "a=http://127.0.0.1:1", *values])

    def test_env_and_file_keys_are_bound_to_named_upstreams(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"ENGINE_KEY": "secret-a"}):
            key_file = Path(temp) / "key"
            key_file.write_text("secret-b\n", encoding="utf-8")
            upstreams = front._upstreams(self.args("--upstream", "b=http://127.0.0.1:2",
                "--upstream-key-env", "a=ENGINE_KEY", "--upstream-key-file", f"b={key_file}"), frontdoor)
        self.assertEqual([u.key for u in upstreams], ["secret-a", "secret-b"])
        self.assertNotIn("secret-a", repr(upstreams))
        self.assertNotIn("secret-b", repr(upstreams))

    def test_missing_duplicate_or_unknown_key_is_rejected_without_secret_output(self):
        cases = (("--upstream-key-env", "unknown=ENGINE_KEY"),
                 ("--upstream-key-env", "a=ABSENT_KEY"),
                 ("--upstream-key-env", "a=ENGINE_KEY", "--upstream-key-env", "a=ENGINE_KEY"))
        for values in cases:
            with self.subTest(values=values), patch.dict(os.environ, {"ENGINE_KEY": "private-secret"}, clear=True):
                output = io.StringIO()
                with redirect_stderr(output):
                    self.assertEqual(front.run(self.args(*values)), 2)
                self.assertNotIn("private-secret", output.getvalue())

    def test_models_command_uses_upstream_key_for_catalog_request(self):
        class AuthenticatedEngine(Engine):
            def do_GET(self):
                self.server.credentials.append(self.headers.get("Authorization"))
                if self.headers.get("Authorization") != "Bearer engine-secret":
                    self.send_error(401)
                    return
                super().do_GET()

        server = ThreadingHTTPServer(("127.0.0.1", 0), AuthenticatedEngine)
        server.credentials = []
        with serving(server), patch.dict(os.environ, {"ENGINE_KEY": "engine-secret"}):
            args = self.args("--upstream-key-env", "a=ENGINE_KEY")
            args.upstream = [f"a=http://127.0.0.1:{server.server_port}"]
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(front.run(args), 0)
            self.assertEqual(json.loads(output.getvalue())["data"][0]["id"], "org/model")
            self.assertNotIn("engine-secret", output.getvalue())
        self.assertEqual(server.credentials, ["Bearer engine-secret"])

    def test_urls_do_not_silently_drop_embedded_credentials_or_query(self):
        for url in ("http://user:password@localhost:1", "http://localhost:1?key=secret", "http://localhost:1#x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                frontdoor.parse_upstream("a=" + url)


if __name__ == "__main__":
    unittest.main()
