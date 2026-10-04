import io
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cli_chat


class ClientEventTests(unittest.TestCase):
    def setUp(self):
        self.client = object.__new__(cli_chat.CodexClient)
        self.client.stdin = io.StringIO()
        self.client.stdout = io.StringIO()
        self.client.stderr = io.StringIO()
        self.client.streamed = set()
        self.client.turn_done = False
        self.client.turn_status = "unknown"
        self.sent = []
        self.client.send = self.sent.append

    def test_stream_and_completion_do_not_duplicate_agent_text(self):
        self.client._event({"method": "item/agentMessage/delta", "params": {
            "itemId": "m", "delta": "OK"}})
        self.client._event({"method": "item/completed", "params": {
            "item": {"id": "m", "type": "agentMessage", "text": "OK"}}})
        self.client._event({"method": "turn/completed", "params": {
            "turn": {"status": "completed"}}})
        self.assertEqual(self.client.stdout.getvalue(), "OK\n")
        self.assertTrue(self.client.turn_done)

    def test_noninteractive_approval_fails_closed(self):
        self.client._event({"id": 7, "method": "item/commandExecution/requestApproval",
                            "params": {"command": "echo example"}})
        self.assertEqual(self.sent, [{"id": 7, "result": {"decision": "decline"}}])
        self.client._event({"id": 8, "method": "mcpServer/elicitation/request", "params": {}})
        self.assertEqual(self.sent[-1]["error"]["code"], -32601)

    def test_model_validation(self):
        self.assertTrue(cli_chat.MODELS.fullmatch("jev-auto"))
        self.assertTrue(cli_chat.MODELS.fullmatch("effortlane-auto"))
        self.assertEqual(cli_chat.display_model("jev-shadow"), "Effortlane Shadow")
        self.assertTrue(cli_chat.MODELS.fullmatch("gpt-6-sol"))
        self.assertFalse(cli_chat.MODELS.fullmatch("bad;command"))

    def test_turn_displays_public_alias_when_executor_is_unavailable(self):
        self.client.root = Path("/missing")
        self.client.thread_id = "thread"
        self.client.model = "jev-auto"
        self.client.request = Mock(return_value={})
        self.client._read = Mock(return_value={"method": "turn/completed", "params": {
            "turn": {"status": "completed"}}})
        self.assertEqual(self.client.turn("hello"), "completed")
        self.assertEqual(self.client.stderr.getvalue(), "[Effortlane Auto]\n")
        self.assertNotIn("jev-auto", self.client.stderr.getvalue())

    def test_public_model_alias_is_normalized_before_client_creation(self):
        client = Mock()
        client.close = Mock()
        client.initialize = Mock()
        with (patch.object(cli_chat, "CodexClient", return_value=client) as constructor,
              patch("sys.stdin", io.StringIO("")),
              patch("sys.stderr", io.StringIO())):
            self.assertEqual(cli_chat.run(["--model", "effortlane-shadow", "--once"], Path("/tmp")), 0)
        self.assertEqual(constructor.call_args.args[1], "effortlane-shadow")

    def test_last_resumes_thread_before_turn(self):
        thread = "01a0e77c-b9c3-7961-8201-79edce3ffc49"
        self.client.model = "jev-auto"
        self.client.request = lambda method, params: (
            {"data": [{"id": thread}]} if method == "thread/list" else
            {"thread": {"id": thread}} if method == "thread/resume" else {})
        self.assertEqual(self.client.initialize(last=True), thread)
        self.assertEqual(self.client.thread_id, thread)
        self.assertEqual(self.sent[-1], {"method": "initialized", "params": {}})


if __name__ == "__main__":
    unittest.main()
