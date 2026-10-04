import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import transport


CAPABILITY = "a" * 48
EVENT = b'data: {"type":"response.completed","response":{"usage":{"input_tokens":7,"output_tokens":3}}}\n\n'


class FakeRouter:
    def __init__(self, catalog_path):
        self.catalog_path = catalog_path
        self.calls = []
        self.records = []
        self.decision = {"model": "gpt-5.6-luna", "effort": "low", "mode": "auto",
                         "reason": "jev", "proposed_model": "gpt-5.6-luna"}

    def decide(self, payload, client, session_id, native_selection=False, mode_override=None):
        self.calls.append((payload, client, session_id, native_selection, mode_override))
        return self.decision.copy()

    def record_usage(self, decision, usage, status):
        self.records.append((decision, usage, status))


class FakeResponse:
    status = 200

    def __init__(self):
        self.parts = [EVENT[:11], EVENT[11:45], EVENT[45:]]

    def getheaders(self):
        return [("Content-Type", "text/event-stream"), ("x-codex-turn-state", "sticky"),
                ("Transfer-Encoding", "chunked")]

    def getheader(self, name, default=None):
        return dict((key.lower(), value) for key, value in self.getheaders()).get(name.lower(), default)

    def read(self, amount):
        return self.parts.pop(0) if self.parts else b""

    read1 = read


class FakeConnection:
    calls = []

    def __init__(self, host, timeout):
        self.host = host
        self.timeout = timeout
        self.headers = []
        FakeConnection.calls.append(self)

    def putrequest(self, method, path, **options):
        self.method, self.path, self.options = method, path, options

    def putheader(self, name, value):
        self.headers.append((name.lower(), value))

    def endheaders(self, body=None):
        self.body = body

    def getresponse(self):
        return FakeResponse()

    def close(self):
        pass


class TransportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        catalog = Path(self.temp.name) / "catalog.json"
        catalog.write_text(json.dumps({"models": [{"slug": "gpt-6-sol", "visibility": "list",
                                                     "supported_reasoning_levels": [{"effort": "medium"}]}]}))
        self.router = FakeRouter(catalog)
        self.server = transport.RouterServer(("127.0.0.1", 0), CAPABILITY, self.router)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        FakeConnection.calls.clear()
        self.patch = mock.patch.object(transport.http.client, "HTTPSConnection", FakeConnection)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def test_manual_request_is_byte_identical_and_native_headers_forward(self):
        body = b'{ "model": "gpt-6-astra", "input": [{"type":"message","content":"hello"}], "tools": [] }'
        status, headers, result = self.request("POST", f"/{CAPABILITY}/responses", body, {
            "Authorization": "Bearer native", "ChatGPT-Account-Id": "account",
            "x-codex-turn-state": "state", "thread-id": "thread", "X-Jev-Client": "desktop"})
        self.assertEqual(status, 200)
        self.assertEqual(result, EVENT)
        self.assertEqual(headers["x-codex-turn-state"], "sticky")
        call = FakeConnection.calls[-1]
        self.assertEqual((call.host, call.path), ("chatgpt.com", "/backend-api/codex/responses"))
        self.assertEqual(call.body, body)
        self.assertIn(("authorization", "Bearer native"), call.headers)
        self.assertIn(("chatgpt-account-id", "account"), call.headers)
        self.assertIn(("x-codex-turn-state", "state"), call.headers)
        self.assertNotIn("x-jev-client", [key for key, _ in call.headers])
        self.assertEqual(len(self.router.calls), 1)
        self.assertEqual(self.router.records[0][2], "ok")

    def test_both_generations_of_aliases_rewrite_to_native_models(self):
        for model, mode in (("jev-auto", "auto"), ("effortlane-auto", "auto"),
                            ("jev-shadow", "shadow"), ("effortlane-shadow", "shadow")):
            body = json.dumps({"model": model, "input": []}).encode()
            rewritten, decision = transport._rewrite(body, self.router, "cli", "legacy")
            self.assertEqual(json.loads(rewritten)["model"], "gpt-6-sol")
            self.assertTrue(decision['_alias'])
            self.assertEqual(self.router.calls[-1][-1], mode)

    def test_alias_rewrites_only_model_effort_and_records_usage(self):
        original = {"model": "jev-auto", "reasoning": {"effort": "high", "summary": "auto"},
                    "input": [{"type": "message", "content": "test"}], "tools": [{"type": "function", "name": "run"}],
                    "parallel_tool_calls": True, "prompt_cache_key": "opaque"}
        status, _, result = self.request("POST", f"/{CAPABILITY}/responses", json.dumps(original),
                                         {"Thread-Id": "thread-1", "X-Jev-Client": "desktop", "Authorization": "Bearer native"})
        self.assertEqual((status, result), (200, EVENT))
        sent = json.loads(FakeConnection.calls[-1].body)
        self.assertEqual(sent.pop("model"), "gpt-6-sol")
        self.assertEqual(sent["reasoning"].pop("effort"), "medium")
        original.pop("model")
        original["reasoning"].pop("effort")
        self.assertEqual(sent, original)
        self.assertEqual(self.router.calls[0][1:], ("desktop", "thread-1", False, "auto"))
        decision, usage, status = self.router.records[0]
        self.assertEqual((decision["reason"], decision["proposed_model"]),
                         ("requires_native_model_selection", "gpt-5.6-luna"))
        self.assertEqual((usage["input_tokens"], status), (7, "ok"))

    def test_capability_cli_path_sets_client_without_changing_native_headers(self):
        body = b'{"model":"gpt-6-sol","input":[]}'
        headers = {"Authorization": "Bearer native", "ChatGPT-Account-Id": "account",
                   "Originator": "chatgpt-desktop", "Thread-Id": "thread"}
        status, _, _ = self.request("POST", f"/{CAPABILITY}/cli/responses", body, headers)
        self.assertEqual(status, 200)
        self.assertEqual(self.router.calls[0][1], "cli")
        self.assertEqual(self.router.records[0][0]["client"], "cli")
        call = FakeConnection.calls[-1]
        self.assertEqual(call.path, "/backend-api/codex/responses")
        self.assertIn(("authorization", "Bearer native"), call.headers)
        self.assertIn(("chatgpt-account-id", "account"), call.headers)
        self.assertIn(("originator", "chatgpt-desktop"), call.headers)
        self.assertEqual(self.request("POST", "/wrong/cli/responses", body, headers)[0], 404)
        self.assertEqual(self.request("POST", f"/{CAPABILITY}/cli/unknown", body, headers)[0], 404)
        self.assertEqual(self.request("GET", f"/{CAPABILITY}/cli/models?client_version=0.155.0",
                                      headers={"Authorization": "Bearer native"})[0], 200)

    def test_cli_route_token_links_usage_to_wrapper_decision(self):
        token = "a1b2c3d4e5f60708"
        body = b'{"model":"gpt-6-sol","input":[]}'
        headers = {"Authorization": "Bearer native", "Thread-Id": "native-thread"}
        path = f"/{CAPABILITY}/cli/{token}"
        self.assertEqual(self.request("GET", path + "/models", headers=headers)[0], 200)
        self.assertEqual(self.request("POST", path + "/responses", body, headers)[0], 200)
        self.assertEqual(self.router.calls[-1][1:3], ("cli", "cli-" + token))
        self.assertEqual(self.router.records[-1][0]["client"], "cli")
        self.assertEqual(self.router.records[-1][0]["route_id"], transport.cli_route_id(token))
        self.assertEqual(self.request("POST", f"/{CAPABILITY}/cli/bad-token/responses", body, headers)[0], 404)

    def test_health_websocket_and_rejections(self):
        self.assertEqual(self.request("GET", "/health")[0], 200)
        self.assertEqual(self.request("GET", f"/{CAPABILITY}/responses", headers={"Upgrade": "websocket"})[0], 426)
        self.assertEqual(self.request("GET", "/wrong/responses", headers={"Upgrade": "websocket"})[0], 404)
        self.assertEqual(self.request("GET", "/health", headers={"Origin": "https://example.com"})[0], 403)
        self.assertEqual(self.request("GET", "/health", headers={"Host": "example.com"})[0], 403)
        self.assertFalse(FakeConnection.calls)

    def test_server_header_uses_effortlane_brand(self):
        status, headers, _ = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertIn("Effortlane", headers["Server"])
        self.assertNotIn("jev-router", headers["Server"])

    def test_native_zstd_alias_body_is_bounded_and_rewritten(self):
        if transport.zstd is None:
            self.skipTest("stdlib zstd requires Python 3.14")
        original = {"model": "jev-auto", "input": [{"role": "user", "content": "hi"}]}
        compressed = transport.zstd.compress(json.dumps(original).encode())
        status, _, _ = self.request("POST", f"/{CAPABILITY}/responses", compressed,
                                    {"Authorization": "Bearer native", "Content-Encoding": "zstd"})
        self.assertEqual(status, 200)
        sent = json.loads(FakeConnection.calls[-1].body)
        self.assertEqual(sent["model"], "gpt-6-sol")
        self.assertNotIn("content-encoding", [key for key, _ in FakeConnection.calls[-1].headers])

    def test_compaction_endpoint(self):
        status, _, _ = self.request("POST", f"/{CAPABILITY}/responses/compact", b'{"model":"gpt-6-sol"}',
                                    {"Authorization": "Bearer native"})
        self.assertEqual(status, 200)
        self.assertEqual(FakeConnection.calls[-1].path, "/backend-api/codex/responses/compact")

    def test_models_endpoint_for_native_catalog_refresh(self):
        status, _, _ = self.request("GET", f"/{CAPABILITY}/models?client_version=0.155.0",
                                    headers={"Authorization": "Bearer native", "ChatGPT-Account-Id": "account"})
        self.assertEqual(status, 200)
        call = FakeConnection.calls[-1]
        self.assertEqual(call.path, "/backend-api/codex/models?client_version=0.155.0")
        self.assertIn(("chatgpt-account-id", "account"), call.headers)
        self.assertEqual(self.request("GET", f"/{CAPABILITY}/models?evil=1")[0], 400)

    def test_bounded_body_and_concurrency(self):
        path = f"/{CAPABILITY}/responses"
        self.assertEqual(self.request("POST", path, "{}", {"Authorization": "Bearer native", "Content-Length": str(transport.MAX_BODY + 1)})[0], 413)
        self.assertEqual(self.request("POST", path, "{}", {"Authorization": "Bearer native", "Transfer-Encoding": "chunked"})[0], 400)
        # The client can finish reading before the handler's finally releases its slot.
        deadline = time.monotonic() + 2
        while True:
            acquired = 0
            while acquired < self.server.max_inflight and self.server.slots.acquire(blocking=False):
                acquired += 1
            if acquired == self.server.max_inflight:
                break
            for _ in range(acquired):
                self.server.slots.release()
            if time.monotonic() >= deadline:
                self.fail("request slots were not released")
            time.sleep(0.01)
        try:
            self.assertEqual(self.request("POST", path, "{}", {"Authorization": "Bearer native"})[0], 503)
        finally:
            for _ in range(self.server.max_inflight):
                self.server.slots.release()
        self.assertFalse(FakeConnection.calls)

    def test_fifth_request_does_not_fail_during_parallel_codex_work(self):
        # A main turn plus several agents is a normal Desktop workload.
        for _ in range(4):
            self.assertTrue(self.server.slots.acquire(blocking=False))
        try:
            status, _, _ = self.request(
                "POST", f"/{CAPABILITY}/responses", '{"model":"gpt-6-sol","input":[]}',
                {"Authorization": "Bearer native"})
            self.assertEqual(status, 200)
        finally:
            for _ in range(4):
                self.server.slots.release()

    def test_completed_event_releases_slot_without_upstream_eof(self):
        class OpenAfterCompletion(FakeResponse):
            def read1(self, amount):
                if not self.parts:
                    raise AssertionError("read again after terminal SSE event")
                return self.parts.pop(0)
        with mock.patch.object(FakeConnection, "getresponse", return_value=OpenAfterCompletion()):
            status, _, result = self.request(
                "POST", f"/{CAPABILITY}/responses", '{"model":"gpt-6-sol","input":[]}',
                {"Authorization": "Bearer native"})
        self.assertEqual((status, result), (200, EVENT))
        self.assertEqual(len(self.router.records), 1)
        self.assertEqual(self.router.records[0][2], "ok")

    def test_terminal_scan_waits_for_complete_sse_frame(self):
        scan = transport._UsageScan()
        scan.feed(EVENT[:-1])
        self.assertFalse(scan.finished)
        scan.feed(EVENT[-1:])
        self.assertTrue(scan.finished)


if __name__ == "__main__":
    unittest.main()
