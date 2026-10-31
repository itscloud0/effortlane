import io
import json
import os
import random
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import lane
import lane_claude


class LaneClaudeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        env = {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / "cfg"),
            "XDG_STATE_HOME": str(self.home / "state"),
            "XDG_DATA_HOME": str(self.home / "data"),
            "CLAUDE_CONFIG_DIR": str(self.home / "claude"),
            "CODEX_HOME": str(self.home / "codex"),
            "TYPESAFE_API_KEY": "test-key",
            "PATH": os.environ.get("PATH", ""),
        }
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = lane.load_config()
        (self.home / "claude").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def plan(self, args, cwd="/work/app", rng_value=0.9):
        rng = mock.Mock(spec=random.Random)
        rng.random.return_value = rng_value
        return lane_claude.plan_launch(args, self.config, cwd, rng)

    @staticmethod
    def flag(cmd, name):
        return cmd[cmd.index(name) + 1] if name in cmd else None

    # --- launch planning ---------------------------------------------------------

    def test_default_mode_is_shadow_native_model_with_observer(self):
        cmd, record = self.plan(["--add-dir", "x"])
        self.assertEqual(record["arm"], "control")
        self.assertNotIn("--model", cmd)
        self.assertNotIn("--agents", cmd)
        settings = json.loads(self.flag(cmd, "--settings"))
        hook = settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]
        self.assertTrue(hook["command"].endswith("hook --mode shadow"))
        self.assertTrue(hook["async"])
        self.assertIn("statusline", settings["statusLine"]["command"])
        self.assertEqual(cmd[-2:], ["--add-dir", "x"])
        session = self.flag(cmd, "--session-id")
        self.assertEqual(record["session"], lane_claude.session_hash(session))

    def test_lanes_arm_starts_on_standard_lane_with_subagents_and_hints(self):
        cmd, record = self.plan(["--effortlane-auto", "fix the login"])
        self.assertEqual((record["arm"], self.flag(cmd, "--model"), self.flag(cmd, "--effort")),
                         ("lanes", "sonnet", "medium"))
        agents = json.loads(self.flag(cmd, "--agents"))
        self.assertEqual(set(agents), {"scout", "builder", "architect"})
        self.assertEqual((agents["architect"]["model"], agents["scout"]["tools"]), ("opus", ["Read", "Grep", "Glob", "Bash"]))
        self.assertIn("Write", agents["scout"]["disallowedTools"])
        hook = json.loads(self.flag(cmd, "--settings"))["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        self.assertTrue(hook.endswith("hook --mode auto"))
        self.assertEqual(cmd[-1], "fix the login")
        self.assertNotIn("--effortlane-auto", cmd)

    def test_trial_randomizes_new_sessions(self):
        lane._atomic_write(lane.state_dir() / "trial.json",
                           json.dumps({"started": time.time() - 10, "ends": time.time() + 999, "ratio": 0.5}))
        self.assertEqual(self.plan([], rng_value=0.2)[1]["arm"], "lanes")
        self.assertEqual(self.plan([], rng_value=0.7)[1]["arm"], "control")
        self.assertEqual(self.plan([], rng_value=0.2)[1]["mode"], "trial")

    def test_manual_model_is_kept_and_excluded(self):
        cmd, record = self.plan(["--effortlane-auto", "--model", "opus"])
        self.assertEqual(record["arm"], "manual")
        self.assertEqual(cmd.count("--model"), 1)  # only the user's own
        self.assertEqual(self.flag(cmd, "--model"), "opus")

    def test_passthrough_for_subcommands_print_and_off(self):
        for args in (["mcp", "list"], ["--version"], ["-p", "hi"], ["--settings", "x.json"], ["--effortlane-off"]):
            cmd, record = self.plan(args)
            self.assertIsNone(record, args)
            self.assertEqual(cmd, ["claude"] + [a for a in args if a != "--effortlane-off"])

    def test_resume_keeps_the_sessions_arm(self):
        session = "12345678-1234-1234-1234-123456789abc"
        lane_claude._append("sessions.jsonl", {"event": "session", "arm": "lanes",
                                               "session": lane_claude.session_hash(session)})
        cmd, record = self.plan(["--resume", session])
        self.assertEqual((record["arm"], record["resumed"]), ("lanes", True))
        self.assertNotIn("--session-id", cmd)
        self.assertEqual(self.flag(cmd, "--model"), "sonnet")
        project = lane_claude._project_dir("/work/my app")
        project.mkdir(parents=True)
        (project / f"{session}.jsonl").write_text("")
        self.assertEqual(self.plan(["--continue"], cwd="/work/my app")[1]["arm"], "lanes")
        self.assertEqual(self.plan(["-r"])[1]["arm"], "unassigned")

    def test_wrap_falls_back_to_native_when_planning_fails(self):
        with mock.patch.object(lane_claude, "plan_launch", side_effect=RuntimeError), \
                mock.patch("shutil.which", return_value="/bin/claude"), mock.patch("os.execv") as execv, \
                mock.patch("sys.stderr", io.StringIO()):
            lane_claude.wrap(["--effortlane-auto", "hi"])
        execv.assert_called_once_with("/bin/claude", ["claude", "hi"])

    # --- status line ---------------------------------------------------------------

    def test_statusline_records_changes_and_renders_user_line(self):
        (self.home / "claude" / "settings.json").write_text(json.dumps(
            {"statusLine": {"type": "command", "command": "cat >/dev/null; printf mine", "padding": 1}}))
        event = {"session_id": "s1", "model": {"id": "claude-sonnet-5-5"},
                 "cost": {"total_cost_usd": 0.5, "total_lines_added": 10, "total_lines_removed": 2},
                 "rate_limits": {"five_hour": {"used_percentage": 12.5}, "seven_day": {"used_percentage": 40}}}
        for _ in range(3):
            out = io.StringIO()
            lane_claude.statusline(io.StringIO(json.dumps(event)), out)
            self.assertEqual(out.getvalue(), "mine")
        rows = list(lane_claude._rows("statusline.jsonl"))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["h5"], rows[0]["d7"], rows[0]["cost_usd"]), (12.5, 40.0, 0.5))
        self.assertEqual(lane_claude.settings_json("auto")["statusLine"]["padding"], 1)

    def test_statusline_survives_garbage(self):
        out = io.StringIO()
        self.assertEqual(lane_claude.statusline(io.StringIO("nope"), out), 0)

    # --- metrics -------------------------------------------------------------------

    def test_price_uses_cache_tiers(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000,
                 "cache_creation_input_tokens": 2_000_000,
                 "cache_creation": {"ephemeral_5m_input_tokens": 1_000_000, "ephemeral_1h_input_tokens": 1_000_000}}
        self.assertAlmostEqual(lane_claude.price("claude-opus-5-5", usage), 4 + 20 + 0.2 + 5 + 8)
        self.assertAlmostEqual(lane_claude.price("claude-sonnet-5-5", {"cache_creation_input_tokens": 1_000_000}), 2.5)
        self.assertIsNone(lane_claude.price("gpt-6", usage))

    def write_session(self, session, arm, model, requests, prompts, rework=0, cwd="-work"):
        lane_claude._append("sessions.jsonl", {"event": "session", "arm": arm,
                                               "session": lane_claude.session_hash(session)})
        for i in range(prompts):
            lane.log_route({"event": "hook", "session": lane_claude.session_hash(session), "tier": "standard",
                            "rework": i < rework})
        folder = self.home / "claude" / "projects" / cwd
        folder.mkdir(parents=True, exist_ok=True)
        usage = {"input_tokens": 0, "output_tokens": 100_000}
        lines = []
        for i in range(requests):  # every request is written twice, as Claude Code streams do
            row = {"requestId": f"{session}-{i}", "message": {"model": model, "usage": usage}}
            lines += [json.dumps(row), json.dumps(row)]
        (folder / f"{session}.jsonl").write_text("\n".join(lines) + "\n")
        sub = folder / session / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-a.jsonl").write_text(json.dumps(
            {"requestId": f"{session}-sub", "message": {"model": "claude-haiku-5-5", "usage": usage}}) + "\n")

    def test_report_counts_exact_requests_and_subagents(self):
        self.write_session("c1", "control", "claude-opus-5-5", 2, prompts=4, rework=1)
        self.write_session("l1", "lanes", "claude-sonnet-5-5", 2, prompts=4)
        self.write_session("m1", "manual", "claude-opus-5-5", 9, prompts=4)
        sessions = lane_claude.collect(0)["sessions"]
        control = sessions[lane_claude.session_hash("c1")]
        self.assertAlmostEqual(control["cost"], 2 * 2.0 + 0.05)  # 2 Opus requests + 1 Haiku subagent, deduped
        self.assertEqual((control["prompts"], control["rework"]), (4, 1))
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            lane_claude.report(as_json=True)
        data = json.loads(out.getvalue())
        self.assertEqual(data["summary"]["control"]["sessions"], 1)
        self.assertEqual(data["summary"]["lanes"]["cost_usd"], round(2 * 1.0 + 0.05, 4))
        self.assertEqual(data["verdict"], "not enough data")

    def test_verdict_needs_enough_sessions_and_detects_savings(self):
        for i in range(lane_claude.MIN_SESSIONS):
            self.write_session(f"c{i}", "control", "claude-opus-5-5", 3 + i % 2, prompts=5)
            self.write_session(f"l{i}", "lanes", "claude-sonnet-5-5", 3 + i % 2, prompts=5)
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            lane_claude.report(as_json=True)
        data = json.loads(out.getvalue())
        self.assertTrue(data["verdict"].startswith("lanes SAVE"))
        point, low, high = data["cost_per_prompt_change"]
        self.assertLess(high, 0)
        self.assertAlmostEqual(point, -0.48, places=1)
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(lane_claude.report(), 0)

    def test_limit_points_only_from_solo_stretches(self):
        self.write_session("c1", "control", "claude-opus-5-5", 1, prompts=2)
        self.write_session("l1", "lanes", "claude-sonnet-5-5", 1, prompts=2)
        c, l = lane_claude.session_hash("c1"), lane_claude.session_hash("l1")
        rows = [(c, 10, 1), (c, 14, 2), (l, 15, 3), (c, 18, 4), (l, 19, 5), (l, 20, 6)]
        path = lane.state_dir() / "statusline.jsonl"
        with open(path, "a") as handle:
            for session, h5, ts in rows:
                handle.write(json.dumps({"session": session, "h5": h5, "d7": h5, "ts": ts}) + "\n")
        limits = lane_claude.collect(0)["limits"]
        self.assertEqual(limits["control"]["h5"], 4)  # 10 -> 14 only; 14 -> 18 had lanes activity between
        self.assertEqual(limits["lanes"]["h5"], 1)


if __name__ == "__main__":
    unittest.main()
