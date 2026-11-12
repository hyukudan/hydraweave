"""End to end against a stub OpenAI-compatible engine: no GPU, no real backend.

The stub serves /v1/models and /v1/chat/completions (plain and SSE); hydraweave's own client drives it and the
shared OpenAI contract checks the documents on the wire."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hydraweave.bench import client  # noqa: E402
import openai_contract  # noqa: E402

MODEL = "stub-model"


class _Stub(BaseHTTPRequestHandler):
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
            return self._json(200, {"object": "list", "data": [{"id": MODEL, "object": "model",
                                                                "context_length": 4096}]})
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            return self._json(404, {"error": {"message": "not found"}})
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        if request.get("model") != MODEL:
            return self._json(404, {"error": {"message": f"model {request.get('model')} not found"}})
        usage = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
        if not request.get("stream"):
            document = {"id": "chatcmpl-stub", "object": "chat.completion", "model": MODEL, "usage": usage,
                        "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant", "content": "hello world"}}]}
            _Stub.seen.append(json.dumps(document).encode())
            return self._json(200, document)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunks = []
        for piece, finish in (("hello", None), (" world", "stop")):
            event = {"id": "chatcmpl-stub", "object": "chat.completion.chunk", "model": MODEL,
                     "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": finish}]}
            chunks.append(f"data: {json.dumps(event)}\n\n".encode())
        chunks.append(f"data: {json.dumps({'id': 'chatcmpl-stub', 'object': 'chat.completion.chunk', 'choices': [], 'usage': usage})}\n\n".encode())
        chunks.append(b"data: [DONE]\n\n")
        _Stub.seen.extend(chunks)
        for chunk in chunks:
            self.wfile.write(chunk)
            self.wfile.flush()
        self.close_connection = True


class StubEngineEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.endpoint = client.Endpoint("http", "127.0.0.1", cls.server.server_address[1])

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Stub.seen.clear()

    def test_models_listing(self):
        data = client.get_json(self.endpoint, "/v1/models", timeout=5)
        self.assertEqual([m["id"] for m in data["data"]], [MODEL])

    def test_plain_completion_meets_contract(self):
        body = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 4}
        reply = client.chat(self.endpoint, body, timeout=10)
        self.assertTrue(reply.ok, reply.error)
        self.assertEqual(reply.text, "hello world")
        openai_contract.assert_chat_response(json.loads(_Stub.seen[0]))

    def test_streamed_completion_meets_contract(self):
        body = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 4, "stream": True,
                "stream_options": {"include_usage": True}}
        reply = client.chat(self.endpoint, body, timeout=10)
        self.assertTrue(reply.ok, reply.error)
        self.assertEqual(reply.text, "hello world")
        self.assertEqual(reply.finish_reason, "stop")
        self.assertEqual((reply.usage or {}).get("completion_tokens"), 2)
        openai_contract.assert_sse_done(list(_Stub.seen))

    def test_unknown_model_is_an_error_not_an_exception(self):
        reply = client.chat(self.endpoint, {"model": "missing", "messages": []}, timeout=5)
        self.assertFalse(reply.ok)
        self.assertEqual(reply.status, 404)
        self.assertTrue(reply.error)


if __name__ == "__main__":
    unittest.main()
