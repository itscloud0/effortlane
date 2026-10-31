import io
import json
import os
import random
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import lane
import lane_claude
import lane_guard
import lane_xray


def stamp(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def request(rid, ts, model="claude-opus-5-5", read=0, write=0, output=1000, ttl="1h", content=None):
    cache = {"ephemeral_1h_input_tokens": write if ttl == "1h" else 0,
             "ephemeral_5m_input_tokens": write if ttl == "5m" else 0}
    message = {"model": model, "usage": {"input_tokens": 10, "output_tokens": output,
                                         "cache_read_input_tokens": read, "cache_creation_input_tokens": write,
                                         "cache_creation": cache}}
    if content:
        message["content"] = content
    return {"requestId": rid, "timestamp": stamp(ts), "message": message}


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        env = {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / "cfg"),
               "XDG_STATE_HOME": str(self.home / "state"), "CLAUDE_CONFIG_DIR": str(self.home / "claude"),
               "TYPESAFE_API_KEY": "test-key", "PATH": os.environ.get("PATH", "")}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.temp.cleanup)
        self.transcript = self.home / "t.jsonl"

    def write(self, rows):
        self.transcript.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def prompt(self, mode="auto", now=None):
        event = {"session_id": "s1", "prompt": "continue with the migration plan please",
                 "transcript_path": str(self.transcript)}
        out = io.StringIO()
        jev = mock.Mock(side_effect=AssertionError("Jev must not be asked for a blocked prompt"))
        with mock.patch("time.time", return_value=now or time.time()):
            lane.hook(io.StringIO(json.dumps(event)), out, jev=jev, mode=mode)
        return out.getvalue()

    # --- reload guard ----------------------------------------------------------------

    def test_blocks_once_after_expired_large_cache_then_lets_it_through(self):
        last = time.time() - 2 * 3600
        self.write([request("a", last - 60, write=200_000), request("b", last, read=200_000)])
        out = json.loads(self.prompt())
        self.assertEqual(out["decision"], "block")
        self.assertIn("idle 2h 00m", out["reason"])
        self.assertIn("200k tokens", out["reason"])
        self.assertIn("$1.60", out["reason"])  # 200k x $4 x 2 (1 h write)
        self.assertIn("~40 normal steps", out["reason"])
        rows = list(lane_claude._rows("routes.jsonl"))
        self.assertEqual((rows[-1]["event"], rows[-1]["kind"], rows[-1]["tokens"]), ("guard", "reload", 200_010))
        with mock.patch.object(lane, "decide", return_value=lane.Route("standard", ["jev"], "jev")):
            self.assertEqual(self.prompt(), "")  # sent again: goes through
        self.assertEqual(lane_claude._rows("routes.jsonl")[-1]["event"], "hook")

    def test_no_block_when_warm_small_or_shadow(self):
        recent = time.time() - 600
        self.write([request("a", recent, write=200_000)])
        with mock.patch.object(lane, "decide", return_value=lane.Route("standard", ["jev"], "jev")):
            self.assertEqual(self.prompt(), "")  # within the 1 h TTL
            self.write([request("a", time.time() - 7200, write=50_000)])
            self.assertEqual(self.prompt(), "")  # below the threshold
            self.write([request("a", time.time() - 7200, write=200_000)])
            self.assertEqual(self.prompt(mode="shadow"), "")  # control arm never blocks

    def test_five_minute_cache_and_disabled_guard(self):
        self.write([request("a", time.time() - 900, write=200_000, ttl="5m")])
        self.assertIn("block", self.prompt())
        (self.home / "cfg" / "effortlane").mkdir(parents=True)
        (self.home / "cfg" / "effortlane" / "lane.json").write_text(json.dumps({"guards": False}))
        self.write([request("z", time.time() - 900, write=300_000, ttl="5m")])
        with mock.patch.object(lane, "decide", return_value=lane.Route("standard", ["jev"], "jev")):
            self.assertEqual(self.prompt(), "")

    # --- subagent guard --------------------------------------------------------------

    def pretool(self, tool_input, tool="Agent"):
        out = io.StringIO()
        lane_guard.pretool(io.StringIO(json.dumps({"session_id": "s1", "tool_name": tool,
                                                   "tool_input": tool_input})), out)
        return json.loads(out.getvalue()) if out.getvalue() else None

    def test_generic_subagent_gets_the_cheaper_model(self):
        out = self.pretool({"description": "d", "prompt": "p", "subagent_type": "general-purpose"})
        spec = out["hookSpecificOutput"]
        self.assertEqual(spec["updatedInput"], {"description": "d", "prompt": "p",
                                                "subagent_type": "general-purpose", "model": "sonnet"})
        self.assertEqual(spec["permissionDecision"], "allow")
        self.assertEqual(self.pretool({"prompt": "p"}, tool="Task")["hookSpecificOutput"]["updatedInput"]["model"],
                         "sonnet")

    def test_explicit_model_named_agents_and_forks_are_kept(self):
        self.assertIsNone(self.pretool({"prompt": "p", "model": "opus"}))
        self.assertIsNone(self.pretool({"prompt": "p", "subagent_type": "architect"}))
        self.assertIsNone(self.pretool({"prompt": "p", "subagent_type": "fork"}))
        self.assertIsNone(self.pretool({"command": "ls"}, tool="Bash"))

    def test_lanes_settings_add_pretool_hook_and_delegation_prompt(self):
        config = lane.load_config()
        rng = mock.Mock(spec=random.Random)
        cmd, record = lane_claude.plan_launch(["--effortlane-auto"], config, "/w", rng)
        settings = json.loads(cmd[cmd.index("--settings") + 1])
        self.assertTrue(settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"].endswith(" pretool"))
        self.assertEqual(settings["hooks"]["PreToolUse"][0]["matcher"], "Agent|Task")
        self.assertIn("--append-system-prompt", cmd)
        self.assertEqual(cmd[cmd.index("--autocompact") + 1], "200000")
        cmd, _ = lane_claude.plan_launch(["--effortlane-shadow"], config, "/w", rng)
        self.assertNotIn("PreToolUse", json.loads(cmd[cmd.index("--settings") + 1])["hooks"])
        self.assertNotIn("--append-system-prompt", cmd)
        self.assertNotIn("--autocompact", cmd)
        cmd, _ = lane_claude.plan_launch(["--effortlane-auto", "--append-system-prompt", "mine",
                                          "--autocompact", "auto"], config, "/w", rng)
        self.assertEqual((cmd.count("--append-system-prompt"), cmd.count("--autocompact")), (1, 1))

    # --- xray ------------------------------------------------------------------------

    def test_xray_splits_costs_and_finds_reloads_and_subagents(self):
        now = time.time() - 3600
        project = self.home / "claude" / "projects" / "-w"
        sub = project / "s1" / "subagents"
        sub.mkdir(parents=True)
        tool_use = [{"type": "tool_use", "id": "t1", "name": "Read"}]
        tool_result = [{"type": "tool_result", "tool_use_id": "t1",
                        "content": [{"type": "text", "text": "x" * 4000}, {"type": "image", "source": {}}]}]
        rows = [request("r1", now - 4000, write=40_000, content=tool_use),
                {"message": {"role": "user", "content": tool_result}, "timestamp": stamp(now - 3990)},
                request("r2", now - 3900, read=40_000, write=1_000),
                request("r3", now, write=60_000)]  # 65 min later: reload
        (project / "s1.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows + rows[-1:]))
        (sub / "agent-a.jsonl").write_text(json.dumps(request("s", now, write=10_000, output=0)) + "\n")
        t = lane_xray.scan(7)
        self.assertEqual(t["requests"], 4)  # duplicate line counted once
        self.assertEqual(t["reload"]["requests"], 1)
        self.assertAlmostEqual(t["reload"]["excess"], 60_000 * (8 - 0.2) / 1e6)
        self.assertEqual(t["starts"], [40_010])
        self.assertEqual((t["tools"]["Read"], t["images"]), (1000, 1))
        self.assertAlmostEqual(t["sub_expensive"], 10_000 * 8 / 1e6 + 10 * 4 / 1e6)
        self.assertAlmostEqual(t["sub_cheaper"], 10_000 * 4 / 1e6 + 10 * 2 / 1e6)
        self.assertAlmostEqual(sum(t["parts"].values()), t["cost"])
        with mock.patch("sys.stdout", io.StringIO()) as out:
            lane_xray.xray(7)
        self.assertIn("cache reloads after breaks", out.getvalue())


if __name__ == "__main__":
    unittest.main()
