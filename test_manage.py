import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

import manage
import rpc_adapter


class ConfigSurgeryTests(unittest.TestCase):
    def test_brand_flags_and_model_values_preserve_prompts(self):
        self.assertEqual(manage.brand_cli_args(["--effortlane-shadow", "-m", "effortlane-auto",
                                              "exec", "mention effortlane-shadow"]),
                         ["--jev-shadow", "-m", "jev-auto", "exec", "mention effortlane-shadow"])
        self.assertEqual(manage.brand_cli_args(["--model=effortlane-shadow", "--effortlane-off"]),
                         ["--model=jev-shadow", "--jev-off"])

    def test_picker_brand_keeps_alias_ids_and_recognizes_only_exact_legacy(self):
        native = {"models": [{"slug": "gpt-6.1-sol", "display_name": "Sol", "visibility": "list",
                              "supported_reasoning_levels": [{"effort": "medium"}]}]}
        catalog = manage.managed_catalog(native)
        aliases = {m["slug"]: m["display_name"] for m in catalog["models"] if m["slug"].startswith("jev-")}
        self.assertEqual(aliases, {"jev-auto": "Effortlane Auto", "jev-shadow": "Effortlane Shadow"})
        self.assertTrue(manage.is_managed_catalog(catalog, native))
        for model in catalog["models"]:
            if model["slug"] in aliases:
                model["display_name"] = "Jev Auto" if model["slug"] == "jev-auto" else "Jev Shadow"
        self.assertTrue(manage.is_managed_catalog(catalog, native))
        for model in catalog["models"]:
            if model["slug"] in aliases:
                model["description"] = model["description"].replace("Effortlane", "Jev")
        self.assertTrue(manage.is_managed_catalog(catalog, native))
        catalog["models"][-1]["description"] = "user modification"
        self.assertFalse(manage.is_managed_catalog(catalog, native))

    def test_round_trip_preserves_unrelated_changes(self):
        original = 'model = "gpt-6-astra"\n# comment\n[features]\nsearch = true\n'
        before = {key: manage.root_fields(original).get(key) for key in manage.MANAGED_KEYS}
        managed = {"model": 'model = "jev-shadow"\n', "openai_base_url": 'openai_base_url = "http://127.0.0.1"\n'}
        changed = manage.edit_root(original, before, managed)
        changed = changed.replace("search = true", "search = false")
        restored = manage.edit_root(changed, managed, before)
        self.assertEqual(restored, original.replace("search = true", "search = false"))

    def test_conflicting_managed_value_refused(self):
        original = 'model = "gpt-6-astra"\n[features]\nsearch = true\n'
        with self.assertRaisesRegex(ValueError, "config changed at model"):
            manage.edit_root(original, {"model": 'model = "jev-shadow"\n'}, {"model": 'model = "gpt-6-astra"\n'})


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "router"
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.codex = self.bin / "codex"
        self.real = self.base / "real-codex"
        self.real.write_text("binary")
        self.real.chmod(0o700)
        self.codex.symlink_to(self.real)
        self.config = self.base / "config.toml"
        self.config.write_text('model = "gpt-6-astra"\n# owner text\n[features]\nsearch = true\n')
        self.cache = self.base / "models_cache.json"
        sol = {"slug": "gpt-6-sol", "display_name": "Sol", "visibility": "list", "supported_reasoning_levels": [{"effort": "medium"}]}
        self.cache.write_text(json.dumps({"models": [sol, {"slug": "gpt-6-astra", "visibility": "list", "supported_reasoning_levels": [{"effort": "medium"}]}]}))
        self.agent = self.base / "LaunchAgent.plist"
        self.key = self.base / "typesafe-key"
        self.key.write_text("test-only")
        self.key.chmod(0o600)

    def install(self):
        manage.install(self.root, self.config, self.bin, self.cache, self.agent, start=False, key_path=self.key)

    def test_install_refuses_foreign_brand_command(self):
        command = self.bin / "effortlane"
        command.symlink_to(self.bin / "missing-foreign")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.install()
        self.assertEqual(os.readlink(command), str(self.bin / "missing-foreign"))

    def test_install_disable_enable_rollback(self):
        self.install()
        self.assertEqual(os.readlink(self.bin / "effortlane"), str(self.root / "jev-codex"))
        self.assertEqual(os.readlink(self.codex), str(self.root / "jev-codex"))
        self.assertEqual(os.readlink(self.bin / "codex-native"), str(self.root / "codex-native"))
        self.assertEqual((self.root / "config.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "capability").stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(list((self.root / "backups").glob("*.toml"))), 1)
        self.assertIn('model = "gpt-6-astra"', self.config.read_text())
        self.assertNotIn('model_catalog_json', self.config.read_text())
        self.assertNotIn("openai_base_url", self.config.read_text())
        self.assertEqual(manage.load_json(self.root / "config.json")["fallback_model"], "gpt-6-sol")
        self.assertEqual(manage.load_json(self.root / "config.json")["auto_roles"], ["luna", "terra", "sol"])
        self.assertEqual(manage.load_json(self.root / "config.json")["auto_policy"], "completion_v4")
        self.assertEqual(manage.load_json(self.root / "config.json")["shadow_policy"], "completion_v4")
        manage.disable(self.root, stop=False)
        self.assertNotIn("openai_base_url", self.config.read_text())
        self.assertIn('model = "gpt-6-astra"', self.config.read_text())
        manage.enable(self.root, start=False)
        self.config.write_text(self.config.read_text().replace("search = true", "search = false"))
        manage.rollback(self.root, stop=False)
        self.assertFalse((self.bin / "effortlane").is_symlink())
        self.assertEqual(os.readlink(self.codex), str(self.real))
        self.assertIn("search = false", self.config.read_text())
        self.assertNotIn("openai_base_url", self.config.read_text())

    def test_rollback_preserves_foreign_brand_command(self):
        self.install()
        command = self.bin / "effortlane"
        command.unlink()
        command.symlink_to(self.bin / "foreign")
        manage.rollback(self.root, stop=False)
        self.assertEqual(os.readlink(command), str(self.bin / "foreign"))

    def test_alias_advertises_one_effort_and_explains_auto(self):
        native = {"models": [{"slug": "gpt-6-sol", "display_name": "Sol", "visibility": "list",
                              "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"},
                                                             {"effort": "high"}]}]}
        aliases = {item["slug"]: item for item in manage.managed_catalog(native)["models"]
                   if item["slug"].startswith("jev-")}
        self.assertEqual(aliases["jev-auto"]["supported_reasoning_levels"], [{"effort": "medium"}])
        self.assertIn("Effortlane chooses", aliases["jev-auto"]["description"])
        self.assertEqual([level["effort"] for level in aliases["jev-shadow"]["supported_reasoning_levels"]],
                         ["low", "medium", "high"])
        self.assertEqual(aliases["jev-shadow"]["default_reasoning_level"], "medium")

    def test_newer_sol_catalog_refresh_uses_newer_alias_metadata(self):
        older = {"slug": "gpt-6-sol", "visibility": "list",
                 "supported_reasoning_levels": [{"effort": "medium"}], "model_messages": {"identity": "old"}}
        newer = {"slug": "gpt-6.1-sol", "visibility": "list",
                 "supported_reasoning_levels": [{"effort": "medium"}, {"effort": "high"}],
                 "default_reasoning_level": "high", "model_messages": {"identity": "new"}}
        catalog = {"models": [older, newer]}
        self.assertEqual(manage.select_sol(catalog), "gpt-6.1-sol")
        aliases = {item["slug"]: item for item in manage.managed_catalog(catalog)["models"]
                   if item["slug"].startswith("jev-")}
        self.assertEqual(aliases["jev-auto"]["model_messages"], newer["model_messages"])
        self.assertEqual(aliases["jev-auto"]["supported_reasoning_levels"], [{"effort": "medium"}])
        self.assertEqual(aliases["jev-shadow"]["supported_reasoning_levels"],
                         [{"effort": "medium"}, {"effort": "high"}])
        self.assertEqual(aliases["jev-shadow"]["default_reasoning_level"], "medium")

    def test_enable_migrates_legacy_relay_url_and_preserves_manual_alias(self):
        self.install()
        manage.disable(self.root, stop=False)
        manifest_path = self.root / "manifest.json"
        manifest = manage.load_json(manifest_path)
        manifest["managed_root"]["openai_base_url"] = 'openai_base_url = "http://127.0.0.1:43191/old"\n'
        manage.write_json(manifest_path, manifest)
        self.config.write_text(self.config.read_text().replace('model = "gpt-6-astra"',
                                                        'model = "jev-auto"'))
        manage.enable(self.root, start=False)
        current = self.config.read_text()
        self.assertIn('model = "jev-auto"', current)
        self.assertNotIn('model_catalog_json = ', current)
        self.assertNotIn("openai_base_url", current)
        migrated = manage.load_json(manifest_path)
        self.assertIsNone(migrated["managed_root"]["openai_base_url"])
        self.assertIn("model", migrated["preserved_user_changes"])

    def test_desktop_runtime_distinguishes_direct_and_adapted_app_server(self):
        sample = """10 1 /Applications/ChatGPT.app/Contents/MacOS/ChatGPT
11 10 /Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex -c x=y app-server
12 10 /opt/homebrew/bin/python3.14 /tmp/rpc_adapter.py --native /Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex --root /tmp -- -c x=y app-server
13 12 /Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex -c x=y app-server
14 13 /opt/homebrew/bin/node_repl
15 14 /opt/homebrew/bin/python3.14 /tmp/rpc_adapter.py --native /Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex --root /tmp -- app-server
"""
        native = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex")
        with mock.patch.object(manage.subprocess, "run", return_value=mock.Mock(stdout=sample)):
            self.assertEqual(manage.desktop_runtime(native), {"app_running": True,
                                                       "adapter_active": True,
                                                       "direct_native_app_server": True})
        sample = "\n".join(line for line in sample.splitlines() if not line.startswith("11 "))
        with mock.patch.object(manage.subprocess, "run", return_value=mock.Mock(stdout=sample)):
            self.assertFalse(manage.desktop_runtime(native)["direct_native_app_server"])
        sample = "\n".join(line for line in sample.splitlines() if not line.startswith("12 "))
        with mock.patch.object(manage.subprocess, "run", return_value=mock.Mock(stdout=sample)):
            self.assertFalse(manage.desktop_runtime(native)["adapter_active"])
        signed = """10 1 /Applications/ChatGPT.app/Contents/MacOS/ChatGPT
11 10 /Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex app-server
"""
        with mock.patch.object(manage.subprocess, "run", return_value=mock.Mock(stdout=signed)):
            self.assertTrue(manage.desktop_runtime(native)["direct_native_app_server"])
            self.assertFalse(manage.desktop_runtime(native)["adapter_active"])
        signed += "12 11 /opt/homebrew/bin/python3.14 /tmp/desktop_bootstrap.py --sidecar --root /tmp --to-native-fd 4 --from-native-fd 5\n"
        with mock.patch.object(manage.subprocess, "run", return_value=mock.Mock(stdout=signed)):
            self.assertTrue(manage.desktop_runtime(native)["adapter_active"])

    def test_doctor_accepts_intentionally_native_desktop(self):
        self.install()
        with (mock.patch.object(manage, "health", return_value=True),
              mock.patch.object(manage, "desktop_env", return_value=""),
              mock.patch.object(manage, "desktop_runtime", return_value={
                  "app_running": True, "adapter_active": False,
                  "direct_native_app_server": True})):
            current = manage.status(self.root)
            self.assertFalse(current["desktop"]["auto_routing_active"])
            self.assertFalse(current["desktop"]["env_present"])
            self.assertTrue(manage.doctor(self.root)["checks"]["desktop_adapter_active"])
        with (mock.patch.object(manage, "health", return_value=True),
              mock.patch.object(manage, "desktop_env", return_value=""),
              mock.patch.object(manage, "desktop_runtime", return_value={
                  "app_running": True, "adapter_active": True,
                  "direct_native_app_server": True})):
            pending_restart = manage.doctor(self.root)
            self.assertFalse(pending_restart["ok"])
            self.assertIn("fully quit and reopen", pending_restart["issues"][0])

    def test_desktop_refresh_native_after_app_update(self):
        self.install()
        self.mock_desktop_launchctl(None)
        manage.desktop_enable(self.root, self.base / "desktop-env.plist", self.real)
        wrapper = self.root / "app-server-wrapper"
        old = self.real
        new = self.base / "codex-cli" / "bin" / "codex"
        new.parent.mkdir(parents=True)
        new.write_text('#!/bin/sh\nprintf "new-native:%s\\n" "$*"\n')
        new.chmod(0o700)
        old.unlink()
        with mock.patch.object(manage, "health", return_value=True):
            before = manage.doctor(self.root)
        self.assertFalse(before["checks"]["native_binary_executable"])
        self.assertFalse(before["checks"]["desktop_wrapper_matches_native_target"])
        self.assertIn("native_target missing", before["issues"][0])
        self.assertIn("wrapper points to missing", before["issues"][1])
        self.assertIn("native_target missing", manage.status(self.root)["native_binary_issue"])
        result = manage.desktop_refresh_native(new, self.root)
        self.assertTrue(result["changed"])
        self.assertEqual(manage.load_json(self.root / "manifest.json")["native_target"], str(new))
        self.assertTrue(manage.doctor(self.root)["checks"]["desktop_wrapper_matches_native_target"])
        backup = Path(result["backup"])
        self.assertEqual(manage.load_json(backup / "manifest.json")["native_target"], str(old))
        self.assertIn(str(old).encode(), (backup / "app-server-wrapper").read_bytes())
        (self.root / "rpc_adapter.py").unlink()  # Exercise the native fallback without starting the adapter.
        run = subprocess.run([str(wrapper), "app-server"], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("new-native:", run.stdout)
        manifest_bytes = (self.root / "manifest.json").read_bytes()
        wrapper_bytes = wrapper.read_bytes()
        backups = list((self.root / "backups").glob("desktop-native-*"))
        self.assertFalse(manage.desktop_refresh_native(new, self.root)["changed"])
        self.assertEqual((self.root / "manifest.json").read_bytes(), manifest_bytes)
        self.assertEqual(wrapper.read_bytes(), wrapper_bytes)
        self.assertEqual(list((self.root / "backups").glob("desktop-native-*")), backups)

    def test_desktop_refresh_refuses_manually_edited_wrapper(self):
        self.install()
        self.mock_desktop_launchctl(None)
        manage.desktop_enable(self.root, self.base / "desktop-env.plist", self.real)
        wrapper = self.root / "app-server-wrapper"
        wrapper.write_bytes(wrapper.read_bytes() + b"# owner change\n")
        new = self.base / "new-codex"
        new.write_text("binary")
        new.chmod(0o700)
        self.assertIn("manually edited", manage.status(self.root)["desktop"]["wrapper_issue"])
        self.assertFalse(manage.doctor(self.root)["checks"]["desktop_wrapper_matches_native_target"])
        with self.assertRaisesRegex(ValueError, "Refusing automatic overwrite"):
            manage.desktop_enable(self.root)
        with self.assertRaisesRegex(ValueError, "Refusing automatic overwrite"):
            manage.desktop_refresh_native(new, self.root)
        self.assertEqual(manage.load_json(self.root / "manifest.json")["native_target"], str(self.real))
        self.assertIn(b"# owner change", wrapper.read_bytes())
        self.assertFalse(list((self.root / "backups").glob("desktop-native-*")))

    def test_rollback_preserves_manual_model(self):
        self.install()
        self.config.write_text(self.config.read_text().replace('model = "gpt-6-astra"', 'model = "custom"'))
        manage.rollback(self.root, stop=False)
        self.assertIn('model = "custom"', self.config.read_text())
        self.assertNotIn("openai_base_url", self.config.read_text())
        self.assertEqual(manage.load_json(self.root / "manifest.json")["preserved_user_changes"], ["model"])

    def test_cli_astra_requires_allowlist(self):
        self.install()
        import core
        decision = {"model": "gpt-6-astra", "effort": "high", "mode": "auto", "reason": "jev"}
        with mock.patch.object(manage, "health", return_value=True), mock.patch.object(core.Router, "decide", side_effect=lambda *args, **kwargs: dict(decision)):
            disabled = manage.cli_args(["exec", "Review architecture"], self.root)
            self.assertEqual(disabled[disabled.index("-m") + 1], "gpt-6-sol")
            config_path = self.root / "config.json"
            config = json.loads(config_path.read_text())
            config["auto_roles"].append("astra")
            config_path.write_text(json.dumps(config))
            enabled = manage.cli_args(["exec", "Review architecture"], self.root)
            self.assertEqual(enabled[enabled.index("-m") + 1], "gpt-6-astra")

    def test_wrapper_routes_only_initial_prompt_and_preserves_overrides(self):
        self.install()
        with mock.patch.object(manage, "health", return_value=True):
            import core
            with mock.patch.object(core.Router, "decide", return_value={"model": "gpt-6-astra", "effort": "high"}) as decide:
                args = manage.cli_args(["exec", "--json", "Fix tests"], self.root, stdin_tty=False)
                self.assertEqual(args[0], str(self.real))
                self.assertEqual(args[1], "-c")
                self.assertTrue(args[2].startswith('openai_base_url="http://127.0.0.1:43191/'))
                self.assertRegex(args[2], r'/cli/[0-9a-f]{16}"$')
                token = args[2].rsplit('/', 1)[-1].rstrip('"')
                self.assertEqual(decide.call_args.kwargs["session_id"], "cli-" + token)
                from core import cli_route_id
                recorded = json.loads((self.root / "state/telemetry.jsonl").read_text().splitlines()[-1])
                self.assertEqual(recorded["route_id"], cli_route_id(token))
                self.assertEqual(args[3:7], ["-m", "gpt-6-sol", "-c", 'model_reasoning_effort="high"'])
                self.assertIn("model_catalog_json=" + manage.toml_string(str(manage.cli_catalog_path(self.root, "native-models.json"))), args)
                self.assertIn("Fix tests", args)
                self.assertEqual(decide.call_args.kwargs["mode_override"], "auto")
                self.assertEqual(decide.call_args.kwargs["native_selection"], True)
            with mock.patch.object(core.Router, "decide", return_value={"model": "gpt-6-sol", "effort": "low"}) as decide:
                args = manage.cli_args(["--jev-auto", "-c", "model_reasoning_effort=high", "exec", "Fix tests"], self.root)
                self.assertEqual(args.count("model_reasoning_effort=high"), 1)
                self.assertIn("gpt-6-sol", args)
                decide.assert_called_once()
            with mock.patch.object(core.Router, "decide", return_value={"model": "gpt-6-sol", "effort": "medium",
                                                                     "proposed_model": "gpt-6-luna", "proposed_effort": "low"}) as decide:
                args = manage.cli_args(["--jev-shadow", "-c", "model_reasoning_effort=xhigh", "exec", "Fix tests"], self.root)
                self.assertEqual(args.count("model_reasoning_effort=xhigh"), 1)
                self.assertEqual(decide.call_args.kwargs["mode_override"], "shadow")
                self.assertNotIn("requested_effort", decide.call_args.args[0])
                record = json.loads((self.root / "state/telemetry.jsonl").read_text().splitlines()[-1])
                self.assertEqual((record["effort"], record["proposed_effort"]), ("xhigh", "low"))
            with mock.patch.object(core.Router, "decide", return_value={"model": "gpt-6-sol", "effort": "low"}) as decide:
                args = manage.cli_args(["exec", "-m", "jev-auto", "Fix tests"], self.root)
                self.assertIn("gpt-6-sol", args)
                self.assertNotIn("jev-auto", args)
                decide.assert_called_once()
            self.assertEqual(manage.cli_args(["-m", "gpt-6-sol", "exec", "Fix"], self.root)[-4:], ["-m", "gpt-6-sol", "exec", "Fix"])
            self.assertEqual(manage.cli_args(["-c", 'model="gpt-6-sol"', "exec", "Fix"], self.root)[-4:], ["-c", 'model="gpt-6-sol"', "exec", "Fix"])
            self.assertEqual(manage.cli_args(["exec", "resume", "id", "continue"], self.root)[-4:], ["exec", "resume", "id", "continue"])
            self.assertEqual(manage.cli_args(["login"], self.root), [str(self.real), "login"])
            self.assertEqual(manage.cli_args(["--help"], self.root), [str(self.real), "--help"])
            self.assertEqual(manage.cli_args(["-c", 'openai_base_url="https://custom.example"', "exec", "Fix"], self.root),
                             [str(self.real), "-c", 'openai_base_url="https://custom.example"', "exec", "Fix"])

    def test_stdin_and_daemon_failure(self):
        self.install()
        bypass = str(self.bin / "codex-native")
        with mock.patch.object(manage, "health", return_value=False):
            args = manage.cli_args(["exec", "-"], self.root, stdin_tty=False)
            self.assertEqual(args, [bypass, "-m", "gpt-6-sol", "exec", "-"])
            args = manage.cli_args(["exec", "Fix"], self.root)
            self.assertEqual(args[:3], [bypass, "-m", "gpt-6-sol"])
            self.assertEqual(manage.cli_args(["--jev-off", "exec", "Fix"], self.root), [bypass, "exec", "Fix"])
            self.assertEqual(manage.cli_args(["-c", 'openai_base_url="https://custom.example"', "exec", "Fix"], self.root),
                             [str(self.real), "-c", 'openai_base_url="https://custom.example"', "exec", "Fix"])
        native = manage.native_args(["exec", "Fix"], self.root)
        self.assertEqual(native[0], str(self.real))
        self.assertFalse(any("openai_base_url=" in arg for arg in native))
        self.assertEqual(native[-4:], ["-m", "gpt-6-sol", "exec", "Fix"])
        native = manage.native_args(["-c", "model_reasoning_effort=high", "exec", "Fix"], self.root)
        self.assertEqual(native[3:5], ["-m", "gpt-6-sol"])
        self.config.write_text('openai_base_url = "http://127.0.0.1:43191/legacy"\n' + self.config.read_text())
        self.assertIn('openai_base_url="https://chatgpt.com/backend-api/codex"',
                      manage.native_args(["exec", "Fix"], self.root))

    def test_cli_route_failure_preserves_native_model_metadata(self):
        self.install()
        import core
        with mock.patch.object(manage, "health", return_value=True), \
             mock.patch.object(core.Router, "decide", side_effect=TimeoutError("Jev unavailable")):
            args = manage.cli_args(["exec", "Fix tests"], self.root)
        self.assertEqual(args[args.index("-m") + 1], "gpt-6-sol")
        self.assertIn("model_catalog_json=" + manage.toml_string(
            str(manage.cli_catalog_path(self.root, "native-models.json"))), args)

    def test_global_shadow_alias_is_default_in_cli_and_keeps_effort(self):
        self.install()
        self.config.write_text(self.config.read_text().replace('model = "gpt-6-astra"',
                                                        'model = "jev-shadow"\nmodel_reasoning_effort = "high"'))
        with mock.patch.object(manage, "health", return_value=True):
            self.assertEqual(manage.status(self.root)["codex_default"],
                             {"model": "jev-shadow", "effort": "high", "routing": "shadow"})
        import core
        decision = {"model": "gpt-6-sol", "effort": "medium", "mode": "shadow",
                    "proposed_model": "gpt-6-luna", "proposed_effort": "low"}
        with mock.patch.object(manage, "health", return_value=True), \
             mock.patch.object(core.Router, "decide", return_value=decision) as decide:
            self.assertIn("jev-shadow", manage.cli_bridge_args([], self.root))
            args = manage.cli_args(["exec", "Reply OK"], self.root)
            self.assertEqual(decide.call_args.kwargs["mode_override"], "shadow")
            self.assertEqual(args[args.index("-m") + 1], "gpt-6-sol")
            receipt = json.loads((self.root / "state/telemetry.jsonl").read_text().splitlines()[-1])
            self.assertEqual((receipt["effort"], receipt["proposed_effort"]), ("high", "low"))
            manage.cli_args(["--jev-auto", "exec", "Reply OK"], self.root)
            self.assertEqual(decide.call_args.kwargs["mode_override"], "auto")
            manual = manage.cli_args(["-m", "gpt-6-sol", "exec", "Reply OK"], self.root)
            self.assertEqual(manual[-4:], ["-m", "gpt-6-sol", "exec", "Reply OK"])

    def test_interactive_cli_keeps_local_auto_alias_and_resume(self):
        self.install()
        with mock.patch.object(manage, "health", return_value=True):
            interactive = manage.cli_args([], self.root)
            self.assertEqual(interactive[-2:], ["-m", "jev-auto"])
            self.assertEqual(manage.cli_args(["exec", "-"], self.root)[-4:], ["-m", "jev-auto", "exec", "-"])
            resume = manage.cli_args(["resume", "--last"], self.root)
            self.assertEqual(resume[-2:], ["resume", "--last"])
            self.assertNotIn("-m", resume)

    def test_remote_control_uses_native_catalog_without_routing_alias(self):
        self.install()
        with mock.patch.object(manage, "health", return_value=True):
            args = manage.cli_args(["remote-control", "start"], self.root)
            self.assertIn('model_catalog_json=' + manage.toml_string(str(manage.cli_catalog_path(self.root, "native-models.json"))), args)
            self.assertNotIn('jev-auto', args)
            self.assertNotIn('jev-shadow', args)
            self.assertEqual(args[-2:], ['remote-control', 'start'])

    def test_native_tui_bridge_preserves_interactive_auto_and_resume(self):
        self.install()
        router_config = manage.load_json(self.root / "config.json")
        router_config["mode"] = "auto"
        manage.write_json(self.root / "config.json", router_config)
        catalog_args = manage._cli_alias_catalog_args(self.root)
        thread = "01a0cab0-65a9-7233-8a01-9e7df612e94b"
        with mock.patch.object(manage, "health", return_value=True):
            self.assertEqual(manage.cli_bridge_args([], self.root), [*catalog_args, "-m", "jev-auto"])
            self.assertEqual(manage.cli_bridge_args(["resume", thread], self.root),
                             [*catalog_args, "-m", "jev-auto", "resume", thread])
            self.assertIsNone(manage.cli_bridge_args(["exec", "Fix tests"], self.root))
            self.assertIsNone(manage.cli_bridge_args(["-m", "gpt-6-sol"], self.root))
            self.assertIsNone(manage.cli_bridge_args(['--config=model="gpt-6-sol"'], self.root))
            self.assertEqual(manage.cli_bridge_args(["resume", "--last"], self.root),
                             [*catalog_args, "-m", "jev-auto", "resume", "--last"])
            self.assertEqual(manage.cli_bridge_args(["--jev-shadow"], self.root),
                             [*catalog_args, "-m", "jev-shadow"])
            self.assertIsNone(manage.cli_bridge_args(["--remote", "ws://127.0.0.1:12"], self.root))
            self.config.write_text(self.config.read_text().replace('model = "gpt-6-astra"', 'model = "gpt-6-sol"'))
            self.assertEqual(manage.cli_bridge_args([], self.root), [*catalog_args, "-m", "jev-auto"])
            self.config.write_text('model = "jev-auto"\nmodel_provider = "other"\n')
            self.assertIsNone(manage.cli_bridge_args([], self.root))

    def test_startup_failure_restores_config_and_symlink(self):
        with mock.patch.object(manage, "start_agent", side_effect=RuntimeError("launch failed")), mock.patch.object(manage, "stop_agent"):
            with self.assertRaisesRegex(RuntimeError, "launch failed"):
                manage.install(self.root, self.config, self.bin, self.cache, self.agent, start=True, key_path=self.key)
        self.assertEqual(os.readlink(self.codex), str(self.real))
        self.assertNotIn("openai_base_url", self.config.read_text())
        self.assertFalse(self.agent.exists())
        self.assertEqual(manage.load_json(self.root / "manifest.json")["config_state"], "failed")

    def test_separate_execution_binary_preserves_original_link(self):
        bundled = self.base / "bundled-codex"
        bundled.write_text("binary")
        bundled.chmod(0o700)
        manage.install(self.root, self.config, self.bin, self.cache, self.agent, start=False,
                       key_path=self.key, execution_binary=bundled)
        manifest = manage.load_json(self.root / "manifest.json")
        self.assertEqual(manifest["native_target"], str(bundled))
        self.assertEqual(manifest["original_codex_link"], str(self.real))
        self.assertEqual(manage.native_args(["exec", "Fix"], self.root)[0], str(bundled))
        manage.rollback(self.root, stop=False)
        self.assertEqual(os.readlink(self.codex), str(self.real))

    def test_cli_target_updates_independently_and_falls_back(self):
        self.install()
        newer = self.base / "updated-codex"
        newer.write_text("binary")
        newer.chmod(0o700)
        changed = manage.cli_set_target(newer, self.root)
        self.assertTrue(changed["changed"])
        self.assertTrue((Path(changed["backup"]) / "manifest.json").is_file())
        self.assertFalse(manage.cli_set_target(newer, self.root)["changed"])
        self.assertEqual(len(list((self.root / "backups").glob("cli-target-*"))), 1)
        with mock.patch.object(manage, "health", return_value=True):
            self.assertEqual(manage.cli_args(["update"], self.root), [str(newer), "update"])
        self.assertEqual(manage.native_args(["--version"], self.root)[0], str(newer))
        manifest = manage.load_json(self.root / "manifest.json")
        self.assertEqual(manifest["native_target"], str(self.real))
        self.assertEqual(manage.status(self.root)["cli_binary"], str(newer))
        self.assertTrue(manage.doctor(self.root)["checks"]["cli_binary_executable"])
        newer.unlink()
        with mock.patch.object(manage, "health", return_value=True):
            self.assertEqual(manage.cli_args(["update"], self.root), [str(self.real), "update"])
        self.assertFalse(manage.doctor(self.root)["checks"]["cli_binary_executable"])
        self.assertIn("falls back", " ".join(manage.doctor(self.root)["issues"]))

    def test_cli_target_rejects_router_wrapper(self):
        self.install()
        with self.assertRaisesRegex(ValueError, "Effortlane wrapper"):
            manage.cli_set_target(self.root / "jev-codex", self.root)
        self.assertEqual(manage.load_json(self.root / "manifest.json")["cli_target"], str(self.real))

    def test_cli_catalog_refresh_adds_new_sol_without_changing_desktop(self):
        self.install()
        desktop_native = (self.root / "native-models.json").read_bytes()
        desktop_managed = (self.root / "models.json").read_bytes()
        catalog = manage.load_json(self.cache)
        catalog["models"].append({"slug": "gpt-6.1-sol", "visibility": "list", "supported_in_api": True,
                                  "supported_reasoning_levels": [{"effort": "medium"}, {"effort": "high"}],
                                  "model_messages": {"identity": "new-sol"}})
        source = self.base / "fresh-account-catalog.json"
        manage.write_json(source, catalog)
        changed = manage.cli_refresh_catalog(source, self.root)
        self.assertEqual(changed["sol"], "gpt-6.1-sol")
        self.assertTrue(changed["changed"])
        self.assertTrue((Path(changed["backup"]) / "manifest.json").is_file())
        self.assertEqual((self.root / "native-models.json").read_bytes(), desktop_native)
        self.assertEqual((self.root / "models.json").read_bytes(), desktop_managed)
        self.assertEqual(manage.select_sol(manage.load_json(manage.cli_catalog_path(self.root, "native-models.json"))),
                         "gpt-6.1-sol")
        self.assertIn("gpt-6.1-sol", [item["slug"] for item in
                      manage.load_json(manage.cli_catalog_path(self.root, "models.json"))["models"]])
        with mock.patch.object(manage, "health", return_value=True):
            self.assertIn(str(manage.cli_catalog_path(self.root, "models.json")),
                          manage.cli_args([], self.root)[4])
            self.assertTrue(manage.doctor(self.root)["checks"]["cli_catalog_matches_latest_sol"])
        with mock.patch.object(manage, "health", return_value=False):
            fallback = manage.cli_args(["--jev-shadow", "exec", "Reply OK"], self.root)
            self.assertEqual(fallback[fallback.index("-m") + 1], "gpt-6.1-sol")
        adapter = rpc_adapter.Adapter(self.root, client="cli",
                                      catalog_path=manage.cli_catalog_path(self.root, "native-models.json"))
        self.assertEqual(adapter.router.catalog_path, manage.cli_catalog_path(self.root, "native-models.json"))
        self.assertFalse(manage.cli_refresh_catalog(source, self.root)["changed"])

    def test_desktop_catalog_refresh_uses_bundled_binary_and_preserves_config(self):
        self.install()
        fresh = manage.load_json(self.cache)
        fresh["models"].append({"slug": "gpt-6.1-sol", "visibility": "list",
                                "supported_reasoning_levels": [{"effort": "medium"}, {"effort": "high"}]})
        before_config = self.config.read_bytes()
        before_native = (self.root / "native-models.json").read_bytes()
        seen = []

        def validate(argv, **kwargs):
            seen.append(argv)
            candidate = Path(argv[2].partition("=")[2].strip('"'))
            return subprocess.CompletedProcess(argv, 0, candidate.read_bytes(), b"")

        with mock.patch.object(manage, "fetch_account_catalog", return_value=fresh) as fetch, \
             mock.patch.object(manage.subprocess, "run", side_effect=validate):
            changed = manage.desktop_refresh_catalog(self.root)
            again = manage.desktop_refresh_catalog(self.root)
        self.assertTrue(changed["changed"])
        self.assertFalse(again["changed"])
        self.assertEqual(changed["sol"], "gpt-6.1-sol")
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual((Path(changed["backup"]) / "native-models.json").read_bytes(), before_native)
        self.assertEqual(len(list((self.root / "backups").glob("desktop-models-*"))), 1)
        self.assertEqual(fetch.call_args.args[0], self.real)
        self.assertEqual(seen[0][0], str(self.real))
        self.assertEqual(manage.select_sol(manage.native_catalog(self.root / "native-models.json")), "gpt-6.1-sol")
        self.assertTrue(manage.doctor(self.root)["checks"]["managed_aliases_match_native_sol"])

    def test_desktop_catalog_refresh_refuses_manual_changes(self):
        self.install()
        managed_path = self.root / "models.json"
        managed = manage.load_json(managed_path)
        managed["models"][-1]["description"] = "user edit"
        manage.write_json(managed_path, managed)
        before = managed_path.read_bytes()
        with mock.patch.object(manage, "fetch_account_catalog") as fetch:
            with self.assertRaisesRegex(ValueError, "modified"):
                manage.desktop_refresh_catalog(self.root)
        fetch.assert_not_called()
        self.assertEqual(managed_path.read_bytes(), before)
        self.assertFalse(list((self.root / "backups").glob("desktop-models-*")))

    def test_cli_account_catalog_uses_isolated_auth_and_preserves_original(self):
        self.install()
        auth = self.base / "auth.json"
        auth.write_text('{"test":"secret"}')
        auth.chmod(0o600)
        original = auth.read_bytes()
        seen = {}

        def debug_models(argv, **kwargs):
            isolated = Path(kwargs["env"]["CODEX_HOME"])
            seen["home"] = isolated
            self.assertEqual(argv, [str(self.real), "debug", "models"])
            self.assertEqual((isolated / "auth.json").read_bytes(), original)
            self.assertEqual((isolated / "auth.json").stat().st_mode & 0o777, 0o600)
            self.assertNotEqual(isolated, self.base)
            return subprocess.CompletedProcess(argv, 0, self.cache.read_bytes(), b"")

        with mock.patch.object(manage.subprocess, "run", side_effect=debug_models):
            catalog = manage.cli_account_catalog(self.root)
        self.assertEqual(manage.select_sol(catalog), "gpt-6-sol")
        self.assertFalse(seen["home"].exists())
        self.assertEqual(auth.read_bytes(), original)
        with mock.patch.object(manage.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"", b"secret")):
            with self.assertRaisesRegex(ValueError, "catalog fetch failed"):
                manage.cli_refresh_catalog(root=self.root)
        self.assertNotIn("cli_catalog_generation", manage.load_json(self.root / "manifest.json"))

    def test_cli_update_refreshes_catalog_only_after_native_update_succeeds(self):
        self.install()
        with mock.patch.object(manage.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as native, \
                mock.patch.object(manage, "cli_refresh_catalog", return_value={"changed": True, "sol": "gpt-6.1-sol"}) as refresh:
            self.assertEqual(manage.cli_update_and_refresh(self.root), 0)
            native.assert_called_once_with([str(self.real), "update"], check=False)
            refresh.assert_called_once_with(root=self.root)
        with mock.patch.object(manage.subprocess, "run", return_value=subprocess.CompletedProcess([], 7)), \
                mock.patch.object(manage, "cli_refresh_catalog") as refresh:
            self.assertEqual(manage.cli_update_and_refresh(self.root), 7)
            refresh.assert_not_called()
        with mock.patch.object(manage.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)), \
                mock.patch.object(manage, "cli_refresh_catalog", side_effect=ValueError("secret details")):
            self.assertEqual(manage.cli_update_and_refresh(self.root), 0)

    def test_cli_update_uses_npm_only_for_verified_global_install(self):
        self.install()
        npm_root = self.base / "lib/node_modules"
        package_bin = npm_root / "@openai/codex/bin/codex.js"
        package_bin.parent.mkdir(parents=True)
        package_bin.write_text("native")
        package_bin.chmod(0o700)
        self.real.unlink()
        self.real.symlink_to(package_bin)
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            if argv == ["/usr/local/bin/npm", "root", "-g"]:
                return subprocess.CompletedProcess(argv, 0, str(npm_root) + "\n", "")
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(manage.shutil, "which", return_value="/usr/local/bin/npm"), \
                mock.patch.object(manage.subprocess, "run", side_effect=run), \
                mock.patch.object(manage, "cli_refresh_catalog", return_value={"changed": False, "sol": "gpt-6-sol"}):
            self.assertEqual(manage.cli_update_and_refresh(self.root), 0)
        self.assertEqual(calls[-1], ["/usr/local/bin/npm", "install", "-g", "@openai/codex@latest"])

        calls.clear()
        def mismatched_run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, str(self.base / "other") + "\n", "")

        with mock.patch.object(manage.shutil, "which", return_value="/usr/local/bin/npm"), \
                mock.patch.object(manage.subprocess, "run", side_effect=mismatched_run), \
                mock.patch.object(manage, "cli_refresh_catalog", return_value={"changed": False, "sol": "gpt-6-sol"}):
            self.assertEqual(manage.cli_update_and_refresh(self.root), 0)
        # A mismatched npm root never authorizes a global package install.
        self.assertEqual(calls[-1], [str(self.real), "update"])

    def test_codex_update_entrypoint_uses_router_update_flow(self):
        self.install()
        with mock.patch.object(manage, "ROOT", self.root), \
                mock.patch.object(manage.sys, "argv", [str(self.codex), "update"]), \
                mock.patch.object(manage, "cli_update_and_refresh", return_value=7) as update:
            with self.assertRaises(SystemExit) as exit_status:
                manage.main()
        self.assertEqual(exit_status.exception.code, 7)
        update.assert_called_once_with()

    def test_claude_entrypoint_passes_native_arguments_to_optional_adapter(self):
        with mock.patch.object(manage, 'ROOT', self.root), \
                mock.patch.object(manage.sys, 'argv', ['effortlane', 'claude', '--', '--model', 'sonnet', '--resume']), \
                mock.patch('claude_shadow.launch', return_value=7) as launch:
            with self.assertRaises(SystemExit) as result:
                manage.main()
        self.assertEqual(result.exception.code, 7)
        launch.assert_called_once_with(['--model', 'sonnet', '--resume'], self.root)

    def mock_desktop_launchctl(self, initial):
        current = {"value": initial}
        calls = []

        def launchctl(*args):
            calls.append(args)
            if args[:2] == ("setenv", "CODEX_CLI_PATH"):
                current["value"] = args[2]
            elif args == ("unsetenv", "CODEX_CLI_PATH"):
                current["value"] = None

        self.enterContext(mock.patch.object(manage, "desktop_env", side_effect=lambda: current["value"]))
        self.enterContext(mock.patch.object(manage, "launchctl", side_effect=launchctl))
        start = self.enterContext(mock.patch.object(manage, "start_desktop_agent"))
        stop = self.enterContext(mock.patch.object(manage, "stop_desktop_agent"))
        return current, calls, start, stop

    def test_desktop_opt_in_disable_enable_rollback(self):
        self.install()
        installed_config = self.config.read_bytes()
        desktop_agent = self.base / "desktop-env.plist"
        prior = "/user/other-codex"
        current, calls, start, stop = self.mock_desktop_launchctl(prior)
        manage.desktop_enable(self.root, desktop_agent, self.real)
        wrapper = self.root / "app-server-wrapper"
        manifest = manage.load_json(self.root / "manifest.json")
        self.assertEqual(current["value"], str(wrapper))
        self.assertEqual(manifest["desktop"]["previous_env"], prior)
        self.assertTrue(desktop_agent.exists())
        self.assertTrue(wrapper.stat().st_mode & 0o100)
        self.assertIn("desktop_bootstrap.py", wrapper.read_text())
        self.assertIn("--native", wrapper.read_text())
        self.assertIn("--root", wrapper.read_text())
        self.assertIn('"$@"', wrapper.read_text())
        start.assert_called_once_with(desktop_agent)

        manage.disable(self.root, stop=False)
        self.assertEqual(current["value"], prior)
        self.assertFalse(desktop_agent.exists())
        self.assertEqual(stop.call_count, 1)
        self.assertFalse(manage.load_json(self.root / "manifest.json")["desktop"]["enabled"])

        manage.enable(self.root, start=False)
        self.assertEqual(self.config.read_bytes(), installed_config)
        self.assertEqual(current["value"], prior)
        self.assertFalse(desktop_agent.exists())
        self.assertFalse(manage.load_json(self.root / "manifest.json")["desktop"]["enabled"])
        manage.rollback(self.root, stop=False)
        self.assertEqual(current["value"], prior)
        self.assertFalse(wrapper.exists())
        self.assertFalse(desktop_agent.exists())
        self.assertEqual(os.readlink(self.codex), str(self.real))
        self.assertEqual([call[0] for call in calls if call[0] in ("setenv", "unsetenv")],
                         ["setenv", "setenv"])

    def test_desktop_wrapper_falls_back_when_adapter_runtime_missing(self):
        native = self.base / "native-fallback"
        native.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        native.chmod(0o700)
        wrapper = self.base / "desktop-wrapper"
        wrapper.write_bytes(manage.desktop_wrapper_content(self.root, native, self.base / "missing-python"))
        wrapper.chmod(0o700)
        result = subprocess.run([str(wrapper), "-c", "features.code_mode_host=true", "app-server"],
                                capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.splitlines(), ["-c", 'openai_base_url="https://chatgpt.com/backend-api/codex"',
                                                       "-c", "features.code_mode_host=true", "app-server"])

    def test_desktop_upgrade_legacy_wrapper_preserves_config_and_rejects_user_edit(self):
        self.install()
        desktop_agent = self.base / "desktop-env.plist"
        current, _, _, _ = self.mock_desktop_launchctl(None)
        wrapper = self.root / "app-server-wrapper"
        test_python = Path(sys.executable)
        legacy = manage.legacy_desktop_wrapper_content(self.root, self.real, test_python)
        wrapper.write_bytes(legacy + b"# user edit\n")
        wrapper.chmod(0o700)
        with self.assertRaisesRegex(ValueError, "wrapper changed"):
            manage.desktop_enable(self.root, desktop_agent, test_python)
        wrapper.write_bytes(legacy)
        manage.desktop_enable(self.root, desktop_agent, test_python)
        self.assertIn("desktop_bootstrap.py", wrapper.read_text())
        self.assertIn("model_catalog_json", self.config.read_text())
        self.assertEqual(current["value"], str(wrapper))
        backups = list((self.root / "backups").glob("desktop-enable-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "app-server-wrapper").read_bytes(), legacy)
        result = manage.desktop_safe(self.root)
        self.assertTrue(result["changed"])
        self.assertNotIn("model_catalog_json", self.config.read_text())
        self.assertIsNone(current["value"])
        self.assertEqual(wrapper.read_bytes(), manage.native_desktop_wrapper_content(self.real))
        self.assertFalse(manage.desktop_safe(self.root)["changed"])
        manage.desktop_enable(self.root, desktop_agent, test_python)
        self.assertIn("desktop_bootstrap.py", wrapper.read_text())

    def test_desktop_safe_preserves_external_wrapper_edit(self):
        self.install()
        desktop_agent = self.base / "desktop-env.plist"
        self.mock_desktop_launchctl(None)
        manage.desktop_enable(self.root, desktop_agent, Path(sys.executable))
        wrapper = self.root / "app-server-wrapper"
        edited = wrapper.read_bytes() + b"# owner customization\n"
        wrapper.write_bytes(edited)
        manage.desktop_safe(self.root)
        self.assertEqual(wrapper.read_bytes(), edited)
        self.assertIn("app-server-wrapper", manage.load_json(self.root / "manifest.json")["preserved_user_changes"])

    def test_desktop_enable_refuses_linked_wrapper(self):
        self.install()
        external = self.base / "external-wrapper"
        external.write_text("#!/bin/sh\nexit 0\n")
        (self.root / "app-server-wrapper").symlink_to(external)
        with self.assertRaisesRegex(ValueError, "symlink"):
            manage.desktop_enable(self.root, self.base / "desktop-env.plist", Path(sys.executable))
        self.assertEqual(external.read_text(), "#!/bin/sh\nexit 0\n")

    def test_desktop_enable_failure_recovers_prior_env_from_journal(self):
        self.install()
        desktop_agent = self.base / "desktop-env.plist"
        prior = "/user/custom-codex  "
        current, _, start, _ = self.mock_desktop_launchctl(prior)

        def partial_start(_):
            current["value"] = str(self.root / "app-server-wrapper")
            prepared = manage.load_json(self.root / "manifest.json")["desktop"]
            self.assertEqual(prepared["phase"], "prepared")
            self.assertEqual(prepared["previous_env"], prior)
            raise RuntimeError("partial bootstrap")

        start.side_effect = partial_start
        with self.assertRaisesRegex(RuntimeError, "partial bootstrap"):
            manage.desktop_enable(self.root, desktop_agent, self.real)
        self.assertEqual(current["value"], prior)
        self.assertFalse(desktop_agent.exists())
        self.assertEqual((self.root / "app-server-wrapper").read_bytes(),
                         manage.native_desktop_wrapper_content(self.real))
        desktop = manage.load_json(self.root / "manifest.json")["desktop"]
        self.assertEqual(desktop["phase"], "disabled")
        self.assertFalse(desktop["opted_in"])

    def test_desktop_disable_preserves_externally_changed_env(self):
        self.install()
        desktop_agent = self.base / "desktop-env.plist"
        current, calls, _, _ = self.mock_desktop_launchctl(None)
        manage.desktop_enable(self.root, desktop_agent, self.real)
        current["value"] = "/user/new-codex"
        manage.disable(self.root, stop=False)
        self.assertEqual(current["value"], "/user/new-codex")
        self.assertFalse(desktop_agent.exists())
        self.assertTrue(manage.load_json(self.root / "manifest.json")["desktop"]["env_changed_externally"])
        self.assertNotIn(("unsetenv", "CODEX_CLI_PATH"), calls)
        manage.enable(self.root, start=False)
        self.assertEqual(current["value"], "/user/new-codex")
        self.assertFalse(desktop_agent.exists())
        manage.rollback(self.root, stop=False)
        self.assertEqual(current["value"], "/user/new-codex")

    def test_desktop_disable_keeps_router_enabled(self):
        self.install()
        desktop_agent = self.base / "desktop-env.plist"
        current, _, _, _ = self.mock_desktop_launchctl(None)
        manage.desktop_enable(self.root, desktop_agent, self.real)
        manage.desktop_disable(self.root)
        self.assertIsNone(current["value"])
        self.assertEqual(manage.load_json(self.root / "manifest.json")["config_state"], "enabled")
        self.assertIn('model = "gpt-6-astra"', self.config.read_text())

    def test_desktop_safe_migrates_legacy_alias_without_overwriting_user_catalog(self):
        self.install()
        manifest_path = self.root / "manifest.json"
        manifest = manage.load_json(manifest_path)
        owned = f'model_catalog_json = "{self.root / "models.json"}"\n'
        manifest["managed_root"]["model_catalog_json"] = owned
        manifest["desktop"] = {"opted_in": True, "enabled": False}
        manage.write_json(manifest_path, manifest)
        self.config.write_text('model = "jev-auto"\n' + owned + '# owner text\n[features]\nsearch = true\n')
        result = manage.desktop_safe(self.root)
        self.assertTrue(result["changed"])
        self.assertEqual((Path(result["backup"]) / "config.toml").read_text().splitlines()[0],
                         'model = "jev-auto"')
        self.assertIn('model = "gpt-6-sol"', self.config.read_text())
        self.assertNotIn("model_catalog_json", self.config.read_text())
        self.assertTrue((self.root / "config.json").exists())
        self.assertFalse(manage.load_json(manifest_path)["desktop"]["opted_in"])
        self.assertFalse(manage.desktop_safe(self.root)["changed"])
        self.config.write_text(self.config.read_text().replace('[features]',
            'model_catalog_json = "/custom/catalog.json"\n[features]'))
        with self.assertRaisesRegex(ValueError, "changed outside router"):
            manage.desktop_safe(self.root)

    def test_desktop_enable_refuses_existing_setter(self):
        self.install()
        desktop_agent = self.base / "desktop-env.plist"
        desktop_agent.write_text("user data")
        current, calls, _, _ = self.mock_desktop_launchctl("/user/other-codex")
        with self.assertRaisesRegex(ValueError, "already exists"):
            manage.desktop_enable(self.root, desktop_agent, self.real)
        self.assertEqual(desktop_agent.read_text(), "user data")
        self.assertEqual(current["value"], "/user/other-codex")
        self.assertEqual(calls, [])

    def test_report_uses_supplied_weights_only(self):
        self.install()
        telemetry = self.root / "state/telemetry.jsonl"
        telemetry.write_text(json.dumps({"event": "route", "client": "cli", "model": "gpt-6-sol",
                                         "proposed_model": "gpt-6-astra", "jev_ms": 25,
                                         "switched": True, "status": "ok"}) + "\n" +
                             json.dumps({"event": "usage", "client": "cli", "model": "gpt-6-sol",
                                         "effort": "medium", "input_tokens": 100,
                                         "cached_input_tokens": 20, "output_tokens": 10,
                                         "usage_missing": False, "status": "ok"}) + "\n")
        no_weights = manage.report(self.root)
        self.assertIsNone(no_weights["counterfactual"]["actual_units"])
        self.assertEqual(no_weights["observed"]["calls"], 1)
        self.assertEqual(no_weights["observed"]["by_client"]["cli"]["input_tokens"], 100)
        self.assertEqual(no_weights["observed"]["weak_quality_signals"], {
            "prior_failed_turns": 0, "manual_overrides": 0, "nonzero_command_exits": 0})
        self.assertEqual(no_weights["routes"], {"decisions": 1, "switches": 1, "jev_ms": 25,
                                                 "proposed_models": {"gpt-6-astra": 1},
                                                 "reasons": {},
                                                 "model_bases": {},
                                                 "by_policy": {"unknown": {"decisions": 1,
                                                                            "proposed_models": {"gpt-6-astra": 1},
                                                                            "work_shapes": {},
                                                                            "confidence": {"samples": 0, "mean": None}}},
                                                 "outcomes": {"linked_turns": 0, "unlinked_routes": 1,
                                                              "by_model": {}, "by_work_shape": {}, "by_policy": {}}})
        weights = {"gpt-6-sol": {"input": 1, "cached_input": 0.5, "output": 2},
                   "gpt-6-astra": {"input": 2, "cached_input": 1, "output": 4}}
        result = manage.report(self.root, weights)
        self.assertEqual(result["counterfactual"]["actual_units"], 110)
        self.assertEqual(result["counterfactual"]["all_astra_units"], 220)

    def test_report_summarizes_valid_choice_confidence_without_assuming_quality(self):
        self.install()
        path = self.root / "state/telemetry.jsonl"
        rows = [
            {"event": "route", "policy": "completion_v2", "jev_confidence": 0.9, "work_shape": "unknown"},
            {"event": "route", "policy": "completion_v2", "jev_confidence": 0.5},
            {"event": "route", "policy": "completion_v2", "jev_confidence": "invalid"},
        ]
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        bucket = manage.report(self.root)["routes"]["by_policy"]["completion_v2"]
        self.assertEqual(bucket["decisions"], 3)
        self.assertEqual(bucket["confidence"], {"samples": 2, "mean": 0.7})
        self.assertEqual(bucket["work_shapes"], {"unknown": 1})

    def test_report_links_only_matching_route_and_turn_metadata(self):
        self.install()
        path = self.root / "state/telemetry.jsonl"
        route = {"event": "route", "route_id": "a" * 24, "session": "b" * 24,
                 "client": "desktop", "policy": "completion_v3", "work_shape": "routine",
                 "model": "gpt-6-terra", "effort": "medium", "model_basis": "jev_work_shape"}
        usage = {"event": "usage", "route_id": "a" * 24, "session": "b" * 24,
                 "client": "desktop", "model": "gpt-6-terra", "effort": "medium",
                 "status": "error", "prior_failed": True, "command_failures": 2,
                 "input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10,
                 "usage_missing": False}
        wrong_session = {**usage, "session": "c" * 24}
        unlinked = {**route, "route_id": "d" * 24, "work_shape": "unknown", "model_basis": None}
        path.write_text("\n".join(json.dumps(row) for row in
                                  (route, wrong_session, usage, unlinked)) + "\n")
        routes = manage.report(self.root)["routes"]
        self.assertEqual(routes["model_bases"], {"jev_work_shape": 1})
        self.assertEqual(routes["outcomes"]["linked_turns"], 1)
        self.assertEqual(routes["outcomes"]["unlinked_routes"], 1)
        result = routes["outcomes"]["by_work_shape"]["routine"]
        self.assertEqual((result["turns"], result["failed_turns"], result["command_failures"],
                          result["prior_failed_turns"], result["cached_input_tokens"]),
                         (1, 1, 2, 1, 80))
        self.assertNotIn("unknown", routes["outcomes"]["by_work_shape"])

    def test_evaluate_filters_current_policy_and_links_only_exact_turns(self):
        self.install()
        now = int(time.time())
        route = {"event": "route", "ts": now, "route_id": "a" * 24,
                 "session": "b" * 24, "client": "desktop", "mode": "auto",
                 "policy": "completion_v4", "model": "gpt-6-sol", "effort": "medium",
                 "proposed_model": "gpt-6-terra", "reason": "cache_hysteresis",
                 "work_shape": "routine", "prompt": "private source"}
        usage = {"event": "usage", "ts": now, "route_id": "a" * 24,
                 "session": "b" * 24, "client": "desktop", "model": "gpt-6-sol",
                 "effort": "medium", "status": "ok", "command_failures": 1,
                 "input_tokens": 100, "cached_input_tokens": 90, "output_tokens": 5}
        wrong = {**usage, "session": "wrong"}
        old = {**route, "route_id": "c" * 24, "ts": now - 100000}
        older_policy = {**route, "route_id": "d" * 24, "policy": "completion_v3"}
        path = self.root / "state/telemetry.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in
                                  (route, wrong, usage, old, older_policy)) + "\n")
        result = manage.evaluate(self.root, hours=24)
        self.assertEqual((result["routes"], result["linked_turns"], result["proposal_held_by_cache"]), (1, 1, 1))
        self.assertEqual(result["executed_models"], {"gpt-6-sol": 1})
        self.assertEqual(result["proposed_models"], {"gpt-6-terra": 1})
        self.assertEqual(result["outcomes_by_executed_model"]["gpt-6-sol"]["nonzero_command_exits"], 1)
        self.assertIsNone(result["quality_equivalent_savings"])
        self.assertNotIn("private source", json.dumps(result))
        self.assertEqual(manage.evaluate(self.root, hours=24, since=now + 1)["routes"], 0)
        with self.assertRaises(ValueError):
            manage.evaluate(self.root, since=-1)
        labels = self.root / "labels.jsonl"
        labels.write_text(json.dumps({"route_id": "a" * 24, "outcome": "rework", "comment": "private source"}) + "\n")
        reviewed = manage.evaluate(self.root, labels_path=labels)
        self.assertEqual(reviewed["human_labels"], {"linked_labeled_turns": 1,
                                                    "by_executed_model": {"gpt-6-sol": {
                                                        "accepted": 0, "rework": 1, "failed": 0}}})
        self.assertNotIn("private source", json.dumps(reviewed))
        labels.write_text(json.dumps({"route_id": "a" * 24, "outcome": "accepted"}) + "\n" +
                          json.dumps({"route_id": "a" * 24, "outcome": "rework"}) + "\n")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            manage.evaluate(self.root, labels_path=labels)
        with self.assertRaises(ValueError):
            manage.evaluate(self.root, hours=0)

    def test_reports_link_route_across_telemetry_rotation(self):
        self.install()
        now = int(time.time())
        route = {"event": "route", "ts": now, "route_id": "a" * 24,
                 "session": "b" * 24, "client": "desktop", "mode": "auto",
                 "policy": "completion_v4", "model": "gpt-6-luna", "effort": "low"}
        usage = {"event": "usage", "ts": now, "route_id": "a" * 24,
                 "session": "b" * 24, "client": "desktop", "mode": "auto",
                 "model": "gpt-6-luna", "effort": "low", "status": "ok",
                 "input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 5}
        state = self.root / "state"
        (state / "telemetry.1.jsonl").write_text(json.dumps(route) + "\n")
        (state / "telemetry.jsonl").write_text(json.dumps(usage) + "\n" +
                                             json.dumps({**usage, "input_tokens": 50, "cached_input_tokens": 40,
                                                         "output_tokens": 3}) + "\n")
        self.assertEqual(manage.report(self.root)["routes"]["outcomes"]["linked_turns"], 1)
        linked = manage.report(self.root)["routes"]["outcomes"]["by_model"]["gpt-6-luna"]
        self.assertEqual((linked["model_calls"], linked["input_tokens"]), (2, 150))
        evaluation = manage.evaluate(self.root, hours=24)
        self.assertEqual((evaluation["routes"], evaluation["linked_turns"]), (1, 1))
        self.assertEqual(evaluation["outcomes_by_executed_model"]["gpt-6-luna"]["model_calls"], 2)
        self.assertEqual(evaluation["auto_model_calls"], {
            "calls": 2, "by_model": {"gpt-6-luna": 2}, "by_client": {"desktop": 2},
            "gateway_blocks": 0})

    def test_trace_explains_auto_route_without_prompt_data(self):
        self.install()
        thread_id = "01a0cbdf-57ca-7de2-b8f5-72b8c74a5568"
        import hashlib
        digest = hashlib.sha256(thread_id.encode()).hexdigest()
        state = self.root / "state"
        (state / "desktop-intent.json").write_text(json.dumps({digest[:32]: {
            "alias": "jev-auto", "actual": "gpt-6-sol", "effort_override": "high",
            "private": "must not appear"}}))
        rows = [
            {"event": "route", "route_id": "a" * 24, "session": digest[:24], "model": "gpt-6-sol", "effort": "high",
             "reason": "privacy_fallback", "mode": "auto", "client": "desktop", "prompt": "secret"},
            {"event": "usage", "session": digest[:24], "model": "gpt-6-sol", "effort": "high",
             "input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 10},
            {"event": "usage", "session": "another", "model": "gpt-6-astra", "effort": "max",
             "input_tokens": 900, "output_tokens": 100},
        ]
        (state / "telemetry.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        result = manage.trace(thread_id, self.root)
        self.assertEqual(result["selection"], {"alias": "jev-auto", "actual": "gpt-6-sol",
                                                "effort_override": "high"})
        self.assertEqual(result["routes"][0]["reason"], "privacy_fallback")
        self.assertEqual(result["routes"][0]["route_id"], "a" * 24)
        self.assertEqual(result["usage_events"], 1)
        self.assertEqual(result["executor_usage"]["gpt-6-sol/high"]["cached_input_tokens"], 60)
        self.assertEqual(manage.route(thread_id, self.root)["model"], "gpt-6-sol")
        self.assertEqual(manage.route(thread_id, self.root)["effort"], "high")
        with (state / "telemetry.jsonl").open("a") as sink:
            sink.write(json.dumps({"event": "usage", "session": digest[:24],
                                   "model": "gpt-6-astra", "effort": "medium", "status": "ok"}) + "\n")
        self.assertEqual((manage.route(thread_id, self.root)["model"],
                          manage.route(thread_id, self.root)["source"]), ("gpt-6-astra", "usage"))
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("astra", json.dumps(result))
        with self.assertRaisesRegex(ValueError, "UUID"):
            manage.trace("../config.json", self.root)

    def test_doctor_detects_catalog_drift_without_changing_files(self):
        self.install()
        cache = manage.load_json(self.cache)
        cache["fetched_at"] = manage.dt.datetime.now(manage.dt.timezone.utc).isoformat()
        manage.write_json(self.cache, cache)
        with mock.patch.object(manage, "status", return_value={"health": True, "desktop": {
                "runtime": {"app_running": False, "adapter_active": False}}}):
            before = (self.root / "models.json").read_bytes()
            healthy = manage.doctor(self.root)
            self.assertTrue(healthy["ok"])
            self.assertEqual((self.root / "models.json").read_bytes(), before)
            cache = manage.load_json(self.cache)
            cache["models"][0]["description"] = "server copy changed"
            manage.write_json(self.cache, cache)
            self.assertTrue(manage.doctor(self.root)["checks"]["account_catalog_matches_installed"])
            cache["models"][0]["priority"] = 99
            manage.write_json(self.cache, cache)
            self.assertTrue(manage.doctor(self.root)["checks"]["account_catalog_matches_installed"])
            cache["models"][0]["supports_reasoning_effort_updates"] = True
            manage.write_json(self.cache, cache)
            drift = manage.doctor(self.root)
            self.assertFalse(drift["checks"]["account_catalog_matches_installed"])
            self.assertIn("account model cache differs", drift["issues"][-1])
            cache["fetched_at"] = (manage.dt.datetime.now(manage.dt.timezone.utc)
                                   - manage.dt.timedelta(hours=1)).isoformat()
            manage.write_json(self.cache, cache)
            self.assertNotIn("account_catalog_matches_installed", manage.doctor(self.root)["checks"])
            native = manage.load_json(self.root / "native-models.json")
            native["models"][0]["model_messages"] = {"base_instructions": "changed"}
            manage.write_json(self.root / "native-models.json", native)
            self.assertFalse(manage.doctor(self.root)["checks"]["managed_aliases_match_native_sol"])

    def test_update_catalog_repairs_cli_only_drift_and_is_idempotent(self):
        self.install()
        cache = manage.load_json(self.cache)
        cache["fetched_at"] = manage.dt.datetime.now(manage.dt.timezone.utc).isoformat()
        cache["models"][0]["supports_reasoning_effort_updates"] = True
        manage.write_json(self.cache, cache)
        with mock.patch.object(manage, "status", return_value={"health": True, "desktop": {
                "runtime": {"app_running": False, "adapter_active": False}}}):
            self.assertFalse(manage.doctor(self.root)["ok"])
            manage.update_catalog(self.root)
            self.assertTrue(manage.doctor(self.root)["ok"])
            backups = list((self.root / "backups").glob("catalog-*"))
            self.assertEqual(len(backups), 1)
            self.assertTrue((backups[0] / "native-models.json").is_file())
            updated = (self.root / "models.json").read_bytes()
            manage.update_catalog(self.root)
            self.assertEqual((self.root / "models.json").read_bytes(), updated)
            self.assertEqual(list((self.root / "backups").glob("catalog-*")), backups)


if __name__ == "__main__":
    unittest.main()
