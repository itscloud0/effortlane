import argparse
import io
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import lane


def jev_answer(choice):
    def client(body, timeout, key):
        client.calls.append((body, timeout, key))
        return {"answers": {"lane": {"choice": choice}}, "usage": {"input_tokens": 40, "output_tokens": 2}}
    client.calls = []
    return client


class LaneTest(unittest.TestCase):
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
            "HERMES_HOME": str(self.home / "hermes"),
            "TYPESAFE_API_KEY": "test-key",
            "PATH": os.environ.get("PATH", ""),
        }
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (("DEFAULT_KEY_FILE", self.home / "key"),
                            ("EFFORTLANE_CONFIG", self.home / "effortlane.json")):
            patcher = mock.patch.object(lane, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.home / "codex").mkdir()
        (self.home / "codex" / "models_cache.json").write_text(json.dumps({"models": [
            {"slug": "gpt-6.1-sol", "visibility": "list"}, {"slug": "gpt-6-sol", "visibility": "list"},
            {"slug": "gpt-6.1-terra", "visibility": "list"}, {"slug": "gpt-6-luna", "visibility": "list"},
            {"slug": "gpt-7-luna", "visibility": "hide"},
        ]}))

    def tearDown(self):
        self.temp.cleanup()

    def config(self):
        return lane.load_config()

    def routes(self):
        path = self.home / "state" / lane.MARKER / "routes.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    # --- decision -----------------------------------------------------------------

    def test_jev_decides_and_receives_only_the_sanitized_task(self):
        client = jev_answer("deep")
        route = lane.decide("fix the login bug in /Users/me/proj/auth/session.py where tokens expire early",
                            "codex", self.config(), client)
        self.assertEqual((route.tier, route.source), ("deep", "jev"))
        self.assertEqual(route.usage, {"jev_input_tokens": 40, "jev_output_tokens": 2})
        body, timeout, key = client.calls[0]
        self.assertEqual((timeout, key), (lane.JEV_TIMEOUT, "test-key"))
        self.assertEqual(set(body["questions"]["lane"]["criteria"]), set(lane.TIERS))
        self.assertNotIn("/Users/me", json.dumps(body))
        self.assertEqual(body["state"]["agent"], "codex")

    def test_missing_key_is_an_error_not_a_silent_local_route(self):
        os.environ.pop("TYPESAFE_API_KEY")
        with self.assertRaises(lane.MissingKey):
            lane.decide("refactor the payment module for clarity", "claude", self.config(), jev_answer("light"))

    def test_key_file_from_effortlane_config(self):
        os.environ.pop("TYPESAFE_API_KEY")
        key = self.home / "custom-key"
        key.write_text("from-file\n")
        lane.EFFORTLANE_CONFIG.write_text(json.dumps({"key_file": str(key)}))
        client = jev_answer("standard")
        lane.decide("refactor the payment module for clarity", "claude", self.config(), client)
        self.assertEqual(client.calls[0][2], "from-file")

    def test_jev_failures_fall_back_to_local_rules(self):
        def timeout(*_):
            raise socket.timeout()
        def broken(*_):
            raise ValueError("boom")
        prompt = "why does the worker deadlock under a race condition"
        for client, reason in ((timeout, "jev-timeout"), (broken, "jev-error"), (jev_answer("opus"), "jev-invalid")):
            route = lane.decide(prompt, "claude", self.config(), client)
            self.assertEqual((route.tier, route.source, route.reasons[0]), ("deep", "fallback", reason))

    def test_pasted_and_short_prompts_are_never_sent(self):
        client = jev_answer("deep")
        for prompt in ("<pasted_content id=x>secret log</pasted_content> explain this", "add a test"):
            route = lane.decide(prompt, "claude", self.config(), client)
            self.assertEqual(route.source, "local")
        self.assertEqual(client.calls, [])

    def test_explicit_overrides_skip_jev(self):
        client = jev_answer("light")
        self.assertEqual(lane.decide("ultrathink about the cache layer", "claude", self.config(), client).tier, "deep")
        self.assertEqual(lane.decide("!light fix it", "claude", self.config(), client).tier, "light")
        self.assertEqual(client.calls, [])

    def test_user_rules_take_precedence(self):
        path = lane.config_path()
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"rules": [{"pattern": "terraform", "tier": "deep"}, {"pattern": "(", "tier": "deep"}]}))
        route = lane.decide("tweak the terraform variable names", "claude", self.config(), jev_answer("light"))
        self.assertEqual((route.tier, route.source), ("deep", "override"))

    # --- local fallback rules -----------------------------------------------------

    def test_local_classifier(self):
        cases = {
            "why does the websocket reconnect loop deadlock under load": "deep",
            "почему падает миграция базы, похоже гонка": "deep",
            "rename getUser to fetchUser in api.ts": "light",
            "переименуй функцию и поправь опечатки": "light",
            "add pagination to the users endpoint": "standard",
            "yes": "standard",
            "rename the field\nTraceback (most recent call last):\n  File x": "standard",
            "explain the security model and the migration plan": "deep",
        }
        for prompt, tier in cases.items():
            self.assertEqual(lane.classify(prompt).tier, tier, prompt)

    # --- launching ----------------------------------------------------------------

    def test_codex_roles_resolve_to_newest_listed_model(self):
        catalog = lane.codex_catalog()
        self.assertEqual(lane.resolve_codex_model("sol", catalog), "gpt-6.1-sol")
        self.assertEqual(lane.resolve_codex_model("luna", catalog), "gpt-6-luna")
        self.assertIsNone(lane.resolve_codex_model("astra", catalog))
        self.assertEqual(lane.resolve_codex_model("gpt-5.6-terra", catalog), "gpt-5.6-terra")

    def test_hermes_skips_codex_roles_for_other_providers(self):
        (self.home / "hermes").mkdir()
        (self.home / "hermes" / "config.yaml").write_text("model:\n  default: x\n  provider: openrouter\n")
        self.assertEqual(lane.choose("hermes", "deep", self.config()), (None, None))
        (self.home / "hermes" / "config.yaml").write_text("model:\n  default: x\n  provider: openai-codex\n")
        self.assertEqual(lane.choose("hermes", "deep", self.config()), ("gpt-6.1-sol", None))

    def test_commands_respect_native_overrides(self):
        self.assertEqual(lane.build_command("claude", "opus", "high", "do it", [], False),
                         ["claude", "--model", "opus", "--effort", "high", "do it"])
        self.assertEqual(lane.build_command("claude", "opus", "high", "", ["--model=haiku"], True),
                         ["claude", "-p", "--effort", "high", "--model=haiku"])
        self.assertEqual(lane.build_command("codex", "gpt-6-luna", "low", "go", ["-c", "model_reasoning_effort=high"], True),
                         ["codex", "exec", "-m", "gpt-6-luna", "-c", "model_reasoning_effort=high", "go"])
        self.assertEqual(lane.build_command("hermes", "gpt-6-luna", None, "go", [], True),
                         ["hermes", "chat", "-m", "gpt-6-luna", "-Q", "-q", "go"])
        self.assertEqual(lane.build_command("hermes", None, None, "", [], False), ["hermes", "chat"])

    def launch_args(self, prompt, **kw):
        return argparse.Namespace(prompt=prompt.split(), tier=kw.get("tier"), print=False,
                                  dry_run=kw.get("dry_run", False), native=[])

    def test_launch_execs_routed_command_and_logs_metadata_only(self):
        with mock.patch("shutil.which", return_value="/bin/claude"), mock.patch("os.execv") as execv, \
                mock.patch("sys.stderr", io.StringIO()):
            lane.launch("claude", self.launch_args("migrate the billing tables to the new schema"), jev_answer("deep"))
        execv.assert_called_once_with("/bin/claude", ["claude", "--model", "opus", "--effort", "high",
                                                      "migrate the billing tables to the new schema"])
        row = self.routes()[-1]
        self.assertEqual((row["tier"], row["source"], row["jev_input_tokens"]), ("deep", "jev", 40))
        self.assertNotIn("billing", json.dumps(row))

    def test_launch_without_key_refuses_unless_tier_given(self):
        os.environ.pop("TYPESAFE_API_KEY")
        err = io.StringIO()
        with mock.patch("sys.stderr", err), mock.patch("os.execv") as execv:
            self.assertEqual(lane.launch("claude", self.launch_args("refactor the router module")), 2)
            execv.assert_not_called()
            self.assertIn("TYPESAFE_API_KEY", err.getvalue())
            self.assertEqual(lane.launch("claude", self.launch_args("x", tier="light", dry_run=True)), 0)

    def test_interactive_launch_uses_standard_without_jev(self):
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            lane.launch("claude", self.launch_args("", dry_run=True), jev_answer("deep"))
        self.assertIn("claude --model sonnet --effort medium", err.getvalue())

    # --- hook ---------------------------------------------------------------------

    def run_hook(self, event, client):
        out = io.StringIO()
        self.assertEqual(lane.hook(io.StringIO(json.dumps(event)), out, client), 0)
        return json.loads(out.getvalue()) if out.getvalue() else None

    def test_hook_hints_only_light_and_deep(self):
        prompt = "please look into the flaky checkout test and fix it properly"
        result = self.run_hook({"prompt": prompt, "session_id": "s1"}, jev_answer("deep"))
        self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("architect", result["hookSpecificOutput"]["additionalContext"])
        self.assertIn("signals: Jev.", result["hookSpecificOutput"]["additionalContext"])
        self.assertIn("scout", self.run_hook({"prompt": prompt}, jev_answer("light"))
                      ["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.run_hook({"prompt": prompt}, jev_answer("standard")))
        self.assertNotIn("s1", json.dumps(self.routes()))

    def test_hook_never_fails(self):
        os.environ.pop("TYPESAFE_API_KEY")
        self.assertIsNone(self.run_hook({"prompt": "refactor the router module"}, jev_answer("deep")))
        self.assertEqual(self.routes()[-1]["source"], "missing_key")
        self.assertEqual(lane.hook(io.StringIO("not json"), io.StringIO()), 0)

    # --- install ------------------------------------------------------------------

    def install_args(self, **kw):
        base = dict(model=None, effort=None, subagent_model=None, no_claude=False, force=False, dry_run=False)
        base.update(kw)
        return argparse.Namespace(**base)

    def test_install_is_idempotent_preserves_settings_and_uninstalls(self):
        settings = self.home / "claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        original = {"model": "opus", "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "mine"}]}],
                                                "PreToolUse": [{"matcher": "Agent", "hooks": []}]}}
        settings.write_text(json.dumps(original))
        with mock.patch("sys.stdout", io.StringIO()):
            lane.install(self.install_args(model="sonnet", subagent_model="sonnet"))
            lane.install(self.install_args())
        data = json.loads(settings.read_text())
        commands = [h["command"] for e in data["hooks"]["UserPromptSubmit"] for h in e["hooks"]]
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0], "mine")
        self.assertIn(lane.MARKER, commands[1])
        self.assertEqual((data["model"], data["env"]["CLAUDE_CODE_SUBAGENT_MODEL"]), ("sonnet", "sonnet"))
        self.assertIn("PreToolUse", data["hooks"])
        for name in ("lane.py", "core.py", "costs.py", "harness/claude/agents/scout.md"):
            self.assertTrue((self.home / "data" / lane.MARKER / name).is_file(), name)
        for name in ("scout", "builder", "architect"):
            self.assertTrue((self.home / "claude" / "agents" / f"{name}.md").is_file())
        self.assertTrue(list(settings.parent.glob("settings.json.*.bak-*")))
        with mock.patch("sys.stdout", io.StringIO()):
            lane.uninstall(argparse.Namespace())
        data = json.loads(settings.read_text())
        self.assertEqual(data["hooks"]["UserPromptSubmit"], original["hooks"]["UserPromptSubmit"])
        self.assertFalse((self.home / "claude" / "agents" / "scout.md").exists())
        self.assertFalse(lane.bin_path().exists())
        self.assertFalse((self.home / "data" / lane.MARKER).exists())

    def test_install_keeps_user_edited_subagents_and_foreign_launcher(self):
        agent = self.home / "claude" / "agents" / "scout.md"
        agent.parent.mkdir(parents=True)
        agent.write_text("mine")
        with mock.patch("sys.stdout", io.StringIO()):
            lane.install(self.install_args())
        self.assertEqual(agent.read_text(), "mine")
        lane.bin_path().write_text("#!/bin/sh\necho other\n")
        with self.assertRaises(SystemExit):
            lane.install(self.install_args())

    def test_install_refuses_invalid_settings(self):
        settings = self.home / "claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text("{broken")
        with self.assertRaises(SystemExit), mock.patch("sys.stdout", io.StringIO()):
            lane.install(self.install_args())
        self.assertEqual(settings.read_text(), "{broken")

    def test_bundled_subagents_have_required_frontmatter(self):
        for path in lane.BUNDLED_AGENTS.glob("*.md"):
            head = path.read_text().split("---")[1]
            for key in ("name:", "description:", "model:", "effort:"):
                self.assertIn(key, head, path.name)

    def test_expensive_default_detection(self):
        self.assertTrue(lane._expensive_default("opus"))
        self.assertTrue(lane._expensive_default("claude-opus-5-5"))
        self.assertFalse(lane._expensive_default("opusplan"))
        self.assertFalse(lane._expensive_default("sonnet"))


if __name__ == "__main__":
    unittest.main()
