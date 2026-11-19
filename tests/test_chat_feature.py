"""Portal HTTP → authenticated local router, admitted jobs and cancellation; no GPU or SSH."""
import http.client
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "bench"))
import support  # noqa: E402
from fake_server import FakeServer  # noqa: E402

from hydraweave.web import features, server  # noqa: E402
from hydraweave.web.api import JobContext  # noqa: E402
from hydraweave.web.features import chat  # noqa: E402


class ChatFeatureTests(unittest.TestCase):
    def setUp(self):
        self.bench = support.Bench(self, token_delay=0.001, length=6)
        self.api = self.bench.api
        features.reset(features.load(("chat",)))
        base = self.bench.server.server.RequestHandlerClass

        class AuthenticatedRouter(base):
            def authorized(self):
                if self.headers.get("Authorization") == "Bearer " + support.KEY:
                    return True
                self._json(401, {"error": {"message": "router key required"}})
                return False

            def do_GET(self):
                if self.authorized():
                    super().do_GET()

            def do_POST(self):
                if self.authorized():
                    super().do_POST()

        self.bench.server.server.RequestHandlerClass = AuthenticatedRouter
        self.portal = server.create_server(0, api=self.api)
        self.addCleanup(self.portal.server_close)
        threading.Thread(target=self.portal.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.portal.shutdown)
        self.addCleanup(self.api.close_features)

    def request(self, method, endpoint, arguments=None, *, csrf=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.portal.server_port, timeout=5)
        headers = {"Host": f"127.0.0.1:{self.portal.server_port}"}
        body = None
        if method == "POST":
            body = json.dumps(arguments).encode()
            headers.update(Origin=f"http://127.0.0.1:{self.portal.server_port}",
                           **{"Content-Type": "application/json", "Content-Length": str(len(body))})
            if csrf:
                headers["X-Hydraweave-CSRF"] = self.portal.csrf_token
        try:
            connection.request(method, "/hydraweave/api/" + endpoint, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def send(self, **changes):
        return self.request("POST", "chat/send", {"profile": "qwen", "messages": [
            {"role": "user", "content": "Hello from the portal"}], "max_tokens": 6, **changes})

    def wait(self, job, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, record = self.request("GET", "jobs/" + job["id"])
            self.assertEqual(status, 200)
            if record["state"] != "running":
                return record
            time.sleep(0.01)
        self.fail("chat job did not finish")

    def test_authenticated_same_origin_http_roundtrip_and_private_key_stays_server_side(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.bench.server.port, timeout=5)
        connection.request("GET", "/v1/models")
        response = connection.getresponse()
        self.assertEqual(response.status, 401)
        response.read()
        connection.close()

        status, document = self.request("GET", "chat")
        self.assertEqual(status, 200)
        self.assertEqual(document["profiles"], [{"id": "qwen", "endpoint": f"http://127.0.0.1:{self.bench.server.port}",
                                              "requires_auth": True, "why": None}])
        self.assertEqual(self.bench.server.requests, [], "GET chat reads launch records without probing the router")
        status, accepted = self.send()
        self.assertEqual(status, 202, accepted)
        self.assertEqual(accepted["resources"], ["halo-1", "router"])
        finished = self.wait(accepted)
        self.assertEqual(finished["state"], "succeeded", finished)
        self.assertEqual(finished["result"]["model"], "fixture-model")
        self.assertEqual(finished["result"]["usage"]["completion_tokens"], 6)
        self.assertTrue(finished["result"]["message"]["content"])
        self.assertEqual([row["path"] for row in self.bench.server.requests], ["/v1/models", "/v1/chat/completions"])
        for row in self.bench.server.requests:
            self.assertEqual(row["headers"]["Authorization"], "Bearer " + support.KEY)
        sent = self.bench.server.requests[-1]["body"]
        self.assertEqual(sent["model"], "fixture-model", "the profile id is not assumed to be a served model id")
        self.assertEqual(sent["messages"], [{"role": "user", "content": "Hello from the portal"}])
        for call in self.api.orchestrate.endpoint.call_args_list:
            self.assertEqual(call, mock.call("qwen", config_path=self.api.config_path))
        visible = json.dumps([document, accepted, finished]) + self.api.audit_path.read_text()
        self.assertNotIn(support.KEY, visible)
        self.assertNotIn("key-router-fixture", visible)
        self.assertEqual(self.api.admission_db.holds(), [])

    def test_csrf_refusal_never_starts_a_router_request(self):
        status, _ = self.request("POST", "chat/send", {"profile": "qwen", "messages": [
            {"role": "user", "content": "hello"}]}, csrf=False)
        self.assertEqual(status, 403)
        self.assertEqual(self.bench.server.requests, [])

    def test_arbitrary_targets_keys_and_unbounded_messages_are_rejected(self):
        invalid = [{"url": "http://127.0.0.1:1"}, {"target": {"target": "fake"}},
                   {"api_key": "browser-key"}, {"target_digest": "f" * 64}, {"resources": ["other"]},
                   {"max_tokens": True}, {"max_tokens": 0}, {"max_tokens": chat.MAX_TOKENS + 1},
                   {"messages": []}, {"messages": [{"role": "tool", "content": "x"}]},
                   {"messages": [{"role": "assistant", "content": "x"}]},
                   {"messages": [{"role": "user", "content": [{"image_url": "http://example.invalid"}]}]},
                   {"messages": [{"role": "user", "content": "x"}] * (chat.MAX_MESSAGES + 1)},
                   {"messages": [{"role": "user", "content": "x" * (chat.MAX_MESSAGE_CHARS + 1)}]},
                   {"messages": [{"role": "user", "content": "x" * 25000}] * 2}]
        for change in invalid:
            with self.subTest(change=list(change)):
                status, answer = self.send(**change)
                self.assertEqual(status, 400, answer)
        for profile, expected in (("other", 409), ("unknown", 404)):
            self.assertEqual(self.send(profile=profile)[0], expected)
        self.assertEqual(self.bench.server.requests, [])
        self.assertEqual(self.api.admission_db.holds(), [])

    def test_frozen_target_and_resources_are_checked_before_network_io(self):
        args = chat.validate(self.api, {"profile": "qwen", "messages": [{"role": "user", "content": "hello"}]})
        self.api._resources_of_profile = lambda *a, **kw: {"different-worker", "router"}
        self.assertEqual(chat.resources(self.api, args), {"halo-1", "router"})
        with self.assertRaisesRegex(PermissionError, "changed"):
            chat.run(self.api, args, JobContext(10))
        self.api._resources_of_profile = lambda *a, **kw: {"halo-1", "router"}
        self.api.orchestrate.endpoint.side_effect = lambda *a, **kw: {
            "url": "http://127.0.0.1:1", "key_name": "key-router-fixture"}
        with self.assertRaisesRegex(PermissionError, "changed"):
            chat.run(self.api, args, JobContext(10))
        self.assertEqual(self.bench.server.requests, [])

    def test_cancel_interrupts_stalled_response_and_releases_admission(self):
        self.bench.server.config.update(stall_after=1, length=200)
        status, job = self.send(max_tokens=200)
        self.assertEqual(status, 202, job)
        deadline = time.monotonic() + 3
        while not any(row["method"] == "POST" for row in self.bench.server.requests) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(any(row["method"] == "POST" for row in self.bench.server.requests))
        self.assertEqual(self.send()[0], 409, "overlapping chat jobs share admission")
        began = time.monotonic()
        status, _ = self.request("POST", "cancel", {"job": job["id"]})
        self.assertIn(status, (200, 202))
        record = self.wait(job)
        self.assertLess(time.monotonic() - began, 3)
        self.assertEqual(record["state"], "cancelled", record)
        self.assertIsNone(record["result"])
        self.assertEqual(self.api.admission_db.holds(), [])
        self.bench.server.config.update(stall_after=None, length=6)
        _, retry = self.send()
        self.assertEqual(self.wait(retry)["state"], "succeeded")

    def test_overlapping_profile_stop_cancels_chat(self):
        self.bench.server.config.update(stall_after=1)
        _, job = self.send()
        hold = self.api.admission_db.acquire("stop", {"halo-1", "router"}, label="down qwen")
        self.addCleanup(self.api.admission_db.release, hold)
        record = self.wait(job)
        self.assertEqual(record["state"], "cancelled", record)
        self.assertIn("stopped", record["error"])

    def test_redirect_never_receives_the_routers_key(self):
        with FakeServer() as elsewhere:
            self.bench.server.config["redirect"] = f"http://127.0.0.1:{elsewhere.port}"
            _, job = self.send()
            record = self.wait(job)
            self.assertEqual(record["state"], "failed")
            self.assertIn("redirect refused", record["error"])
            self.assertEqual(elsewhere.requests, [])

    def test_upstream_echo_of_key_is_redacted_and_oversized_reply_is_refused(self):
        self.bench.server.config["responder"] = lambda body: {"text": "echo " + support.KEY}
        _, job = self.send()
        record = self.wait(job)
        self.assertEqual(record["state"], "succeeded")
        self.assertIn("[redacted]", record["result"]["message"]["content"])
        self.assertNotIn(support.KEY, json.dumps(record) + self.api.audit_path.read_text())
        self.bench.server.config["responder"] = lambda body: {"text": "x" * (chat.MAX_REPLY_CHARS + 1)}
        _, job = self.send()
        record = self.wait(job)
        self.assertEqual(record["state"], "failed")
        self.assertIn("size limit", record["error"])

    def test_router_ignoring_stream_returns_bounded_json_reply(self):
        text = "complete JSON answer"

        def answer(handler, body):
            handler._json(200, {"choices": [{"message": {"role": "assistant", "content": text},
                                             "finish_reason": "stop"}],
                                "usage": {"prompt_tokens": 3, "completion_tokens": 4}})

        self.bench.server.answer = answer
        _, job = self.send()
        record = self.wait(job)
        self.assertEqual(record["state"], "succeeded", record)
        self.assertEqual(record["result"]["message"]["content"], text)
        text = "x" * (chat.MAX_REPLY_CHARS + 1)
        _, job = self.send()
        record = self.wait(job)
        self.assertEqual(record["state"], "failed")
        self.assertIn("size limit", record["error"])


if __name__ == "__main__":
    unittest.main()
