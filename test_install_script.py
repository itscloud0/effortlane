import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest


class InstallScriptTests(unittest.TestCase):
    def run_installer(self, member='effortlane-main/bootstrap.py', curl_exit=0):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'fixture.tar.gz'
            program = b'import sys; print("BOOTSTRAP_ARGS=" + repr(sys.argv[1:]))\n'
            with tarfile.open(archive, 'w:gz') as tar:
                info = tarfile.TarInfo(member)
                info.size = len(program)
                tar.addfile(info, io.BytesIO(program))
            tools = root / 'bin'
            tools.mkdir()
            for name, body in {
                'uname': 'echo Darwin',
                'id': 'echo 501',
                'curl': 'while [ "$#" -gt 0 ]; do if [ "$1" = -o ]; then shift; out=$1; fi; shift; done\n'
                        'if [ "$FAKE_CURL_EXIT" != 0 ]; then exit "$FAKE_CURL_EXIT"; fi\ncp "$FAKE_ARCHIVE" "$out"',
            }.items():
                path = tools / name
                path.write_text('#!/bin/sh\n' + body + '\n')
                path.chmod(0o700)
            env = {**os.environ, 'PATH': str(tools) + ':/usr/bin:/bin', 'EFFORTLANE_PYTHON': sys.executable,
                   'FAKE_ARCHIVE': str(archive), 'FAKE_CURL_EXIT': str(curl_exit), 'TMPDIR': str(root)}
            env.pop('EFFORTLANE_REF', None)
            result = subprocess.run(['/bin/sh', str(Path(__file__).with_name('install.sh')), '--key-file', '/test/key'],
                                    env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
            leftover = list(root.glob('effortlane-install.*'))
            escaped = (root / 'escape.py').exists()
            return result, leftover, escaped

    def test_download_extract_delegate_args_and_clean_temporary_source(self):
        result, leftover, _ = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("BOOTSTRAP_ARGS=['--key-file', '/test/key']", result.stdout)
        self.assertEqual(leftover, [])

    def test_failed_download_never_runs_bootstrap_and_cleans_up(self):
        result, leftover, _ = self.run_installer(curl_exit=22)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Download failed', result.stderr)
        self.assertNotIn('BOOTSTRAP_ARGS=', result.stdout)
        self.assertEqual(leftover, [])

    def test_archive_traversal_is_rejected_before_bootstrap(self):
        result, leftover, escaped = self.run_installer(member='../../escape.py')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unsafe archive member', result.stderr)
        self.assertFalse(escaped)
        self.assertEqual(leftover, [])
