import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import claude_shadow


class ClaudeShadowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.key = self.root / "key"
        self.key.write_text("test")
        (self.root / "config.json").write_text(json.dumps({"key_file": str(self.key)}))

    def tearDown(self):
        self.temp.cleanup()

    def rows(self):
        path = self.root / "state" / "claude-telemetry.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def invoke(self, prompt, client):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            result = claude_shadow.hook(self.root, io.StringIO(json.dumps({"prompt": prompt, "session_id": "secret-session"})), client)
        self.assertEqual((result, stdout.getvalue(), stderr.getvalue()), (0, "", ""))

    def test_exact_choice_body_and_private_proposal(self):
        sent = []
        def client(body, timeout, key):
            sent.append((body, timeout, key))
            return {"answers": {"route": {"choice": "sonnet:high"}}, "usage": {"input_tokens": 3, "output_tokens": 2}}
        self.invoke("Fix the parser without changing its public API", client)
        body, timeout, key = sent[0]
        route = body["questions"]["route"]
        self.assertEqual((body["model"], timeout, key), ("jev-latest", 2.0, self.key))
        self.assertEqual(route["type"], "choice")
        self.assertIsInstance(route["criteria"], dict)
        self.assertIn("sonnet:high", route["criteria"])
        self.assertIn("rework", route["instructions"])
        self.assertNotIn("secret-session", json.dumps(body))
        row = self.rows()[0]
        self.assertEqual((row["outcome"], row["proposed_model"], row["actual_model"]), ("ok", "sonnet", "unknown"))
        self.assertNotIn("prompt", row)
        self.assertNotIn("cwd", row)

    def test_privacy_filtered_secret_and_code_never_call_or_read_key(self):
        client = mock.Mock(side_effect=AssertionError("must not call"))
        self.invoke("token=abcdefghijklmnopqrstuvwxzy0123456789", client)
        self.invoke("Please review this\n```python\nsecret = 'x'\n```", client)
        self.assertFalse(client.called)
        self.assertEqual([row["outcome"] for row in self.rows()], ["privacy_filtered", "privacy_filtered"])

    def test_pasted_content_never_routes_or_loads_key_configuration(self):
        client = mock.Mock(side_effect=AssertionError("must not call"))
        pasted_prompts = (
            "<pasted_content>Benign prose to summarize.</pasted_content>",
            "<PASTED_CONTENT source=clipboard>unknown_secret=unrecognized-value</PASTED_CONTENT>",
            "<pasted_content>def handler(request):\n    return request.value",
            "Please fix this <pasted_content source=clipboard",
            "<PaStEd_CoNtEnT label=\"буфер ✓\">Привет, мир</pAsTeD_cOnTeNt>",
            "Please remove </pasted_content> from this example",
        )
        with mock.patch("claude_shadow._load_config", side_effect=AssertionError("must not load config or key path")):
            for prompt in pasted_prompts:
                self.invoke(prompt, client)
        self.assertFalse(client.called)
        self.assertEqual([row["outcome"] for row in self.rows()], ["privacy_filtered"] * len(pasted_prompts))

    def test_ordinary_short_prompt_still_routes(self):
        client = mock.Mock(return_value={"answers": {"route": {"choice": "haiku:default"}}})
        self.invoke("Fix the parser", client)
        self.assertTrue(client.called)
        self.assertEqual(self.rows()[0]["outcome"], "ok")

    def test_invalid_timeout_and_oversized_events_are_silent(self):
        self.invoke("Please implement this change", lambda *_: {"answers": {"route": {"choice": "gpt:high"}}})
        self.invoke("Please implement this change", lambda *_: (_ for _ in ()).throw(urllib.error.URLError(TimeoutError())))
        self.invoke("Please implement this change", lambda *_: (_ for _ in ()).throw(RuntimeError("network failed")))
        self.invoke("x" * 31_000, lambda *_: self.fail("must not call"))
        self.assertEqual([row["outcome"] for row in self.rows()], ["invalid", "timeout", "error", "privacy_filtered"])

    def test_non_prompt_hook_never_calls_transport(self):
        client = mock.Mock(side_effect=AssertionError("must not call"))
        result = claude_shadow.hook(self.root, io.StringIO(json.dumps({"hook_event_name": "SessionStart", "prompt": "Please implement this change"})), client)
        self.assertEqual(result, 0)
        self.assertFalse(client.called)
        self.assertEqual(self.rows()[0]["event"], "claude_native_event")
        self.assertEqual(self.rows()[0]["hook_event"], "SessionStart")

    def test_hook_process_wall_deadline_exits_silently(self):
        program = (
            "import claude_shadow, sys, time; "
            "claude_shadow.HOOK_WALL_SECONDS = 0.05; "
            "claude_shadow.shadow = lambda *args: time.sleep(10); "
            "raise SystemExit(claude_shadow._hook_main(__import__('pathlib').Path(sys.argv[1])))"
        )
        started = time.monotonic()
        result = subprocess.run([sys.executable, "-c", program, str(self.root)], input=json.dumps({"prompt": "Please implement this change"}),
                                text=True, capture_output=True, timeout=2, check=False)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertLess(time.monotonic() - started, 1.5)

    def test_empty_or_invalid_candidate_configuration_fails_open_without_transport(self):
        (self.root / "config.json").write_text(json.dumps({"key_file": str(self.key), "claude_shadow_candidates": {}}))
        self.invoke("Please implement this change", mock.Mock(side_effect=AssertionError("must not call")))
        self.assertEqual(self.rows()[0]["outcome"], "no_candidates")
        self.assertEqual(claude_shadow.candidates({"claude_shadow_candidates": {"haiku": ["high"]}}), {})

    def test_settings_are_atomic_idempotent_and_refuse_edits(self):
        path = claude_shadow.ensure_settings(self.root)
        self.assertEqual(path, claude_shadow.ensure_settings(self.root))
        path.write_text("{}")
        with self.assertRaisesRegex(ValueError, "refusing"):
            claude_shadow.ensure_settings(self.root)

    def test_owned_settings_upgrade_keeps_a_backup(self):
        path = claude_shadow.ensure_settings(self.root)
        original = path.read_bytes()
        with mock.patch("claude_shadow.settings_content", return_value=b'{"hooks":{}}\n'):
            claude_shadow.ensure_settings(self.root)
        self.assertEqual((self.root / "backups").glob("claude-shadow-settings-*.json").__next__().read_bytes(), original)

    def test_launch_preserves_native_args_and_rejects_print_aliases(self):
        fake = self.root / "claude"
        fake.write_text("")
        with mock.patch("claude_shadow.shutil.which", return_value=str(fake)), mock.patch("claude_shadow.subprocess.run") as run:
            run.side_effect = [subprocess.CompletedProcess([], 0, "2.1.251 (Claude Code)", ""), subprocess.CompletedProcess([], 7)]
            self.assertEqual(claude_shadow.launch(["--model", "sonnet", "--resume", "abc"], self.root), 7)
            self.assertEqual(run.call_args.args[0][-4:], ["--model", "sonnet", "--resume", "abc"])
        for option in ("-p", "-pjson", "--print", "--print=json"):
            with self.assertRaisesRegex(ValueError, "print"):
                claude_shadow.launch([option], self.root)
        with mock.patch("claude_shadow.shutil.which", return_value=str(fake)), mock.patch("claude_shadow.subprocess.run") as run:
            run.side_effect = [subprocess.CompletedProcess([], 0, "2.1.251 (Claude Code)", ""), subprocess.CompletedProcess([], 0)]
            claude_shadow.launch(["--", "-p"], self.root)
            self.assertEqual(run.call_args.args[0][-2:], ["--", "-p"])
        with self.assertRaisesRegex(ValueError, "settings"):
            claude_shadow.launch(['--settings', 'custom.json'], self.root)

    def test_native_process_receives_settings_and_args_without_auth_interception(self):
        fake = self.root / 'native-claude'
        capture = self.root / 'captured.json'
        fake.write_text('#!' + sys.executable + '\n'
                        'import json,pathlib,sys\n'
                        'if sys.argv[1:] == ["--version"]: print("2.1.251 (Claude Code)"); raise SystemExit(0)\n'
                        'pathlib.Path(' + repr(str(capture)) + ').write_text(json.dumps(sys.argv[1:]))\n'
                        'raise SystemExit(7)\n')
        fake.chmod(0o700)
        with mock.patch('claude_shadow.shutil.which', return_value=str(fake)):
            result = claude_shadow.launch(['--model','sonnet','--resume','test-resume'], self.root)
        self.assertEqual(result, 7)
        self.assertEqual(json.loads(capture.read_text()), ['--settings', str(self.root / claude_shadow.SETTINGS_NAME),
                                                         '--model','sonnet','--resume','test-resume'])

    def test_old_or_unrecognized_client_version_refused_before_settings_write(self):
        for response in ("2.1.250 (Claude Code)", "unknown"):
            with mock.patch("claude_shadow.shutil.which", return_value="claude"), mock.patch(
                    "claude_shadow.subprocess.run", return_value=subprocess.CompletedProcess([], 0, response, "")):
                with self.assertRaisesRegex(ValueError, "2.1.251"):
                    claude_shadow.launch([], self.root)
            self.assertFalse((self.root / claude_shadow.SETTINGS_NAME).exists())

    def test_native_metadata_is_silent_private_and_never_calls_jev(self):
        events = [
            {"hook_event_name": "SessionStart", "model": "claude-sonnet-5", "source": "resume"},
            {"hook_event_name": "PostModelSwitch", "from_model": "claude-sonnet-5", "to_model": "claude-opus-5",
             "source": "picker", "context_tokens": 150000, "prompt_cache_warm": True,
             "cache_ttl": "5m", "estimated_cache_write_usd": 2.5},
            {"hook_event_name": "Stop", "effort": {"level": "high"}},
            {"hook_event_name": "StopFailure", "error": "rate_limit"},
        ]
        client = mock.Mock(side_effect=AssertionError("must not route lifecycle"))
        for event in events:
            event.update(session_id="private-session", prompt_id="private-prompt", transcript_path="/private/source.jsonl",
                         last_assistant_message="private output", error_details="secret detail")
            with mock.patch("claude_shadow._load_config", side_effect=AssertionError("no key access")):
                self.assertEqual(claude_shadow.hook(self.root, io.StringIO(json.dumps(event)), client), 0)
        self.assertFalse(client.called)
        raw = json.dumps(self.rows())
        for value in ("private-session", "private-prompt", "/private/source.jsonl", "private output", "secret detail"):
            self.assertNotIn(value, raw)
        report = claude_shadow.report(self.root)
        self.assertEqual(report["events"], 0)
        observed = report["native_observations"]
        self.assertEqual(observed["failures"], {"rate_limit": 1})
        self.assertEqual(observed["effort_observations"], {"high": 1})
        self.assertEqual(len({row["prompt_hash"] for row in self.rows()}), 1)
        self.assertEqual(observed["warm_cache_switches"], 1)
        self.assertEqual(observed["estimated_cache_write_usd"], 2.5)
        self.assertEqual(observed["model_observations"], {"claude-sonnet-5": 1, "claude-opus-5": 1})
        settings = json.loads(claude_shadow.settings_content(self.root))
        self.assertNotIn("PreModelSwitch", settings["hooks"])
        self.assertEqual(set(settings["hooks"]), claude_shadow.NATIVE_EVENTS | {"UserPromptSubmit"})

    def test_native_metadata_rejects_untrusted_fields(self):
        event = {"hook_event_name": "PostModelSwitch", "to_model": "private-repo/API_KEY", "source": "secret",
                 "context_tokens": True, "prompt_cache_warm": "true", "estimated_cache_write_usd": float("nan")}
        claude_shadow.native_event(self.root, event)
        self.assertEqual(set(self.rows()[0]), {"schema_version", "event", "hook_event", "ts"})
        claude_shadow.native_event(self.root, {"hook_event_name": "StopFailure", "error": ["private"]})
        self.assertEqual(self.rows()[1]["error"], "unknown")

    def test_report_rejects_unbounded_values_and_does_not_reflect_private_strings(self):
        state = self.root / 'state'
        state.mkdir()
        row = {'event': 'claude_shadow_proposal', 'ts': int(time.time()), 'outcome': [],
               'proposed_model': 'private-repository', 'proposed_effort': 'secret'}
        (state / 'claude-telemetry.jsonl').write_text(json.dumps(row)+'\n')
        report = claude_shadow.report(self.root)
        self.assertNotIn('private-repository', json.dumps(report))
        self.assertFalse(report['truncated'])
        self.assertEqual(report['outcomes'], {'error': 1})
        for hours in (-1, 0, 721, True):
            with self.assertRaises(ValueError):
                claude_shadow.report(self.root, hours)

    def test_report_uses_rotations_allowlisted_statuses_and_unknown_actual(self):
        state = self.root / "state"
        state.mkdir()
        current = {"event": "claude_shadow_proposal", "ts": 2_000_000_000, "outcome": "ok", "proposed_model": "haiku", "proposed_effort": "default", "jev_latency_ms": 2101}
        rotated = {"event": "claude_shadow_proposal", "ts": 2_000_000_000, "outcome": "untrusted-value", "jev_latency_ms": 9}
        (state / "claude-telemetry.jsonl").write_text(json.dumps(current) + "\n")
        (state / "claude-telemetry.old.jsonl").write_text(json.dumps(rotated) + "\n")
        with mock.patch("claude_shadow.time.time", return_value=2_000_000_001):
            report = claude_shadow.report(self.root, 1)
        self.assertEqual(report["events"], 2)
        self.assertEqual(report["outcomes"], {"ok": 1, "error": 1})
        self.assertEqual(report["candidate_coverage"]["proposed_pairs"], {"haiku:default": 1})
        self.assertEqual(report["actual_model"], {"unknown": 2})
        self.assertEqual(report["latency_ms"]["max"], 2101)
