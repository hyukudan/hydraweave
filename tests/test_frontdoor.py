"""The front door: two stub engines behind one endpoint, routed by model (stdlib unittest, no GPU)."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hydraweave import frontdoor  # noqa: E402
from hydraweave.bench import client  # noqa: E402
import openai_contract  # noqa: E402


def _stub(name, models):
    class Stub(BaseHTTPRequestHandler):
        seen = []

        def log_message(self, *args):
            pass

        def _json(self, status, document):
            body = json.dumps(document).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/v1/models":
                return self._json(200, {"object": "list", "data": [
                    {"id": model, "object": "model", "owned_by": "engine"} for model in models]})
            self._json(404, {"error": {"message": "not found"}})

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            Stub.seen.append((self.path, request, self.headers.get("Authorization")))
            usage = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
            if not request.get("stream"):
                return self._json(200, {
                    "id": "chatcmpl-" + name, "object": "chat.completion", "model": request["model"], "usage": usage,
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": f"from {name}"}}]})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for piece, finish in (("from", None), (" " + name, "stop")):
                event = {"id": "chatcmpl-" + name, "object": "chat.completion.chunk", "model": request["model"],
                         "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": finish}]}
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                self.wfile.flush()
            tail = {"id": "chatcmpl-" + name, "object": "chat.completion.chunk", "choices": [], "usage": usage}
            self.wfile.write(f"data: {json.dumps(tail)}\n\ndata: [DONE]\n\n".encode())
            self.close_connection = True

    return Stub


def _serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _dead_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class FrontDoorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.alpha_handler = _stub("alpha", ["model-a", "shared"])
        cls.beta_handler = _stub("beta", ["model-b", "shared"])
        cls.engines = [_serve(ThreadingHTTPServer(("127.0.0.1", 0), handler))
                       for handler in (cls.alpha_handler, cls.beta_handler)]
        upstreams = [frontdoor.Upstream(name, f"http://127.0.0.1:{engine.server_address[1]}", key="engine-key")
                     for name, engine in zip(("alpha", "beta"), cls.engines)]
        upstreams.append(frontdoor.Upstream("gone", f"http://127.0.0.1:{_dead_port()}"))
        cls.front = _serve(frontdoor.make_server(upstreams, port=0, token="front-token", ttl=60))
        cls.endpoint = client.Endpoint("http", "127.0.0.1", cls.front.server_address[1], key="front-token")

    @classmethod
    def tearDownClass(cls):
        for server in (cls.front, *cls.engines):
            server.shutdown()
            server.server_close()

    def setUp(self):
        self.alpha_handler.seen.clear()
        self.beta_handler.seen.clear()

    def test_models_are_the_union_owned_by_upstream(self):
        data = client.get_json(self.endpoint, "/v1/models", timeout=5)
        owners = {entry["id"]: entry["owned_by"] for entry in data["data"]}
        self.assertEqual(owners, {"model-a": "alpha", "shared": "alpha", "model-b": "beta"})
        self.assertIn("gone", self.front.table.errors())
        self.assertEqual(client.get_json(self.endpoint, "/v1/models/model-b", timeout=5)["owned_by"], "beta")

    def test_plain_request_goes_to_the_engine_that_lists_the_model(self):
        reply = client.chat(self.endpoint, {"model": "model-b", "messages": [{"role": "user", "content": "hi"}]},
                            timeout=10)
        self.assertTrue(reply.ok, reply.error)
        self.assertEqual(reply.text, "from beta")
        self.assertEqual(len(self.beta_handler.seen), 1)
        self.assertEqual(self.alpha_handler.seen, [])
        path, request, authorization = self.beta_handler.seen[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(authorization, "Bearer engine-key")
        openai_contract.assert_chat_response({
            "id": "x", "object": "chat.completion", "model": request["model"],
            "choices": [{"index": 0, "finish_reason": reply.finish_reason or "stop",
                         "message": {"role": "assistant", "content": reply.text}}],
            "usage": reply.usage or {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}})

    def test_stream_passes_through(self):
        body = {"model": "model-a", "messages": [{"role": "user", "content": "hi"}], "stream": True,
                "stream_options": {"include_usage": True}}
        reply = client.chat(self.endpoint, body, timeout=10)
        self.assertTrue(reply.ok, reply.error)
        self.assertEqual(reply.text, "from alpha")
        self.assertEqual(reply.finish_reason, "stop")
        self.assertEqual((reply.usage or {}).get("completion_tokens"), 2)

    def test_replicas_take_turns(self):
        for _ in range(4):
            reply = client.chat(self.endpoint, {"model": "shared", "messages": []}, timeout=10)
            self.assertTrue(reply.ok, reply.error)
        self.assertEqual((len(self.alpha_handler.seen), len(self.beta_handler.seen)), (2, 2))

    def test_unknown_model_is_an_openai_404(self):
        reply = client.chat(self.endpoint, {"model": "missing", "messages": []}, timeout=5)
        self.assertFalse(reply.ok)
        self.assertEqual(reply.status, 404)
        self.assertIn("missing", reply.error)

    def test_token_is_required(self):
        anonymous = client.Endpoint("http", "127.0.0.1", self.front.server_address[1])
        reply = client.chat(anonymous, {"model": "model-a", "messages": []}, timeout=5)
        self.assertEqual(reply.status, 401)
        self.assertEqual(self.alpha_handler.seen, [])

    def test_dead_upstream_is_a_502(self):
        dead = frontdoor.Upstream("dead", f"http://127.0.0.1:{_dead_port()}")
        server = _serve(frontdoor.make_server([dead], port=0, fetch=lambda upstream, timeout: [{"id": "ghost"}]))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        endpoint = client.Endpoint("http", "127.0.0.1", server.server_address[1])
        reply = client.chat(endpoint, {"model": "ghost", "messages": []}, timeout=10)
        self.assertEqual(reply.status, 502)
        self.assertIn("dead", reply.error)

    def test_unknown_model_refresh_is_throttled(self):
        now, calls = [0.0], []

        def fetch(upstream, timeout):
            calls.append(now[0])
            return [{"id": "known"}]
        table = frontdoor.ModelTable([frontdoor.parse_upstream("a=http://x:1")], ttl=10.0, fetch=fetch,
                                     clock=lambda: now[0])
        self.assertIsNone(table.pick("missing"))
        self.assertIsNone(table.pick("missing"))
        self.assertEqual(len(calls), 1)
        now[0] = 2.0
        self.assertIsNone(table.pick("missing"))
        self.assertEqual(len(calls), 2)
        self.assertEqual(table.pick("known").name, "a")

    def test_parse_upstream(self):
        self.assertEqual(frontdoor.parse_upstream("n2=http://10.0.0.2:8080").name, "n2")
        self.assertEqual(frontdoor.parse_upstream("http://10.0.0.2:8080").name, "10.0.0.2:8080")
        with self.assertRaises(ValueError):
            frontdoor.parse_upstream("n2=ftp://x")
        with self.assertRaises(ValueError):
            frontdoor.ModelTable([frontdoor.parse_upstream("a=http://x:1"), frontdoor.parse_upstream("a=http://y:1")])


if __name__ == "__main__":
    unittest.main()
