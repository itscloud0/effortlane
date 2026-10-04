import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import bootstrap
import manage


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.native = self.base / "codex-native"
        self.native.write_text("native")
        self.native.chmod(0o700)

    def test_prepares_only_missing_link_and_preserves_user_link(self):
        bin_dir = self.base / "bin"
        self.assertTrue(bootstrap.prepare_codex_link(bin_dir, self.native))
        self.assertEqual(os.readlink(bin_dir / "codex"), str(self.native))
        self.assertFalse(bootstrap.prepare_codex_link(bin_dir, self.native))
        (bin_dir / "codex").unlink()
        (bin_dir / "codex").write_text("user binary")
        with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
            bootstrap.prepare_codex_link(bin_dir, self.native)

    def test_key_prompt_is_hidden_and_file_is_owner_only(self):
        key = self.base / "config/key"
        with mock.patch.object(bootstrap.Path, "home", return_value=self.base), \
                mock.patch.object(bootstrap.os, "isatty", return_value=True), \
                mock.patch.object(bootstrap.getpass, "getpass", return_value="test-secret"):
            selected = bootstrap.key_file()
        self.assertEqual(selected, self.base / ".config/jev-codex-router/typesafe-api-key")
        self.assertEqual(selected.stat().st_mode & 0o777, 0o600)
        self.assertEqual(selected.read_text(), "test-secret\n")
        key.parent.mkdir()
        key.write_text("test-secret")
        key.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "owner-only"):
            bootstrap.key_file(key)

    def test_missing_key_noninteractive_stops_before_install(self):
        with mock.patch.object(bootstrap.Path, "home", return_value=self.base), \
                mock.patch.object(bootstrap.os, "isatty", return_value=False):
            with self.assertRaisesRegex(ValueError, "run interactively"):
                bootstrap.key_file()

    def test_native_binary_rejects_non_executable(self):
        self.native.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "native Codex CLI missing"):
            bootstrap.native_binary(self.native)

    def test_existing_healthy_install_only_runs_doctor(self):
        root = self.base / "router"
        root.mkdir()
        (root / "manifest.json").write_text("{}")
        with mock.patch.object(manage, "ROOT", root), \
                mock.patch.object(manage, "doctor", return_value={"ok": True, "issues": []}) as doctor, \
                mock.patch.object(bootstrap, "native_binary") as native_binary, \
                mock.patch.object(bootstrap, "key_file") as key_file, \
                mock.patch.object(manage, "fetch_account_catalog") as fetch, \
                mock.patch.object(bootstrap, "prepare_codex_link") as prepare_link, \
                mock.patch.object(manage, "install") as install, \
                mock.patch.object(manage, "write_json") as write_json:
            bootstrap.run(self.native, self.base / "key")
        doctor.assert_called_once_with(root)
        native_binary.assert_not_called()
        key_file.assert_not_called()
        fetch.assert_not_called()
        prepare_link.assert_not_called()
        install.assert_not_called()
        write_json.assert_not_called()

    def test_existing_unhealthy_install_only_runs_doctor_and_reports_action(self):
        root = self.base / "router"
        root.mkdir()
        (root / "manifest.json").write_text("{}")
        with mock.patch.object(manage, "ROOT", root), \
                mock.patch.object(manage, "doctor", return_value={
                    "ok": False, "issues": ["native_target missing or not executable"]
                }) as doctor, \
                mock.patch.object(bootstrap, "native_binary") as native_binary, \
                mock.patch.object(bootstrap, "key_file") as key_file, \
                mock.patch.object(manage, "fetch_account_catalog") as fetch, \
                mock.patch.object(bootstrap, "prepare_codex_link") as prepare_link, \
                mock.patch.object(manage, "install") as install, \
                mock.patch.object(manage, "write_json") as write_json:
            with self.assertRaisesRegex(ValueError, "native_target missing.*effortlane doctor"):
                bootstrap.run(self.native, self.base / "key")
        doctor.assert_called_once_with(root)
        native_binary.assert_not_called()
        key_file.assert_not_called()
        fetch.assert_not_called()
        prepare_link.assert_not_called()
        install.assert_not_called()
        write_json.assert_not_called()

    def test_preflight_failure_happens_before_key_handling(self):
        with mock.patch.object(manage, "ROOT", self.base / "uninstalled-router"), \
                mock.patch.object(bootstrap.sys, "platform", "linux"), \
                mock.patch.object(bootstrap, "key_file") as key_file, \
                mock.patch.object(bootstrap, "native_binary") as native_binary:
            with self.assertRaisesRegex(ValueError, "macOS is required"):
                bootstrap.run(self.native, self.base / "key")
        key_file.assert_not_called()
        native_binary.assert_not_called()

    def test_missing_auth_stops_before_key_handling(self):
        config = self.base / "codex/config.toml"
        config.parent.mkdir()
        config.write_text('model = "gpt-6-sol"\n')
        with mock.patch.object(manage, "ROOT", self.base / "uninstalled-router"), \
                mock.patch.object(manage, "CODEX_CONFIG", config), \
                mock.patch.object(bootstrap.sys, "platform", "darwin"), \
                mock.patch.object(bootstrap, "key_file") as key_file:
            with self.assertRaisesRegex(ValueError, "authentication missing"):
                bootstrap.run(self.native, self.base / "key")
        key_file.assert_not_called()

    def test_setup_uses_existing_auth_and_ends_in_auto_without_touching_desktop(self):
        root = self.base / "router"
        bin_dir = self.base / "bin"
        config = self.base / "codex/config.toml"
        config.parent.mkdir()
        config.write_text('model = "gpt-6-sol"\n')
        (config.parent / "auth.json").write_text("test-only")
        key = self.base / "key"
        key.write_text("test-only")
        key.chmod(0o600)
        catalog = {"models": [{"slug": "gpt-6-sol", "visibility": "list"}]}

        def install(**kwargs):
            self.assertEqual(kwargs["key_path"], key)
            self.assertTrue(kwargs["cache_path"].is_file())
            root.mkdir()
            manage.write_json(root / "config.json", {"mode": "shadow"})

        with mock.patch.object(manage, "ROOT", root), \
                mock.patch.object(manage, "BIN", bin_dir), \
                mock.patch.object(manage, "CODEX_CONFIG", config), \
                mock.patch.object(manage, "fetch_account_catalog", return_value=catalog) as fetch, \
                mock.patch.object(manage, "install", side_effect=install) as native_install, \
                mock.patch.object(manage, "cli_set_target") as set_target, \
                mock.patch.object(manage, "cli_refresh_catalog") as refresh, \
                mock.patch.object(bootstrap.shutil, "which", return_value=str(bin_dir / "codex")):
            bootstrap.run(self.native, key)
        self.assertEqual(manage.load_json(root / "config.json")["mode"], "auto")
        self.assertEqual(os.readlink(bin_dir / "codex"), str(self.native))
        self.assertGreaterEqual(fetch.call_count, 1)
        native_install.assert_called_once()
        if set_target.called:
            set_target.assert_called_once_with(self.native)
            refresh.assert_called_once()


if __name__ == "__main__":
    unittest.main()
