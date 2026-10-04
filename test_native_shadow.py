import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

import native_shadow


class NativeShadowTests(unittest.TestCase):
    def setUp(self):
        routed_env = mock.patch.dict(os.environ, {"EFFORTLANE_ROUTED": "0"})
        routed_env.start()
        self.addCleanup(routed_env.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / 'codex/hooks.json'
        self.path.parent.mkdir()
        (self.root / 'native-shadow-hook.json').write_text('{"enabled":true}')

    def tearDown(self):
        self.tmp.cleanup()

    def test_merge_idempotence_and_disable_preserve_other_hooks(self):
        other = {'hooks': [{'type': 'command', 'command': 'echo unrelated'}]}
        original = {'description': 'User settings', 'hooks': {'Stop': [other], 'UserPromptSubmit': [other]}}
        self.path.write_text(json.dumps(original))
        result = native_shadow.configure(self.root, True, self.path)
        self.assertTrue(result['changed'])
        self.assertTrue((Path(result['backup']) / 'hooks.json').exists())
        installed = json.loads(self.path.read_text())
        self.assertEqual(installed['hooks']['Stop'], [other])
        self.assertEqual(installed['hooks']['UserPromptSubmit'][0], other)
        before = self.path.read_bytes()
        self.assertFalse(native_shadow.configure(self.root, True, self.path)['changed'])
        self.assertEqual(self.path.read_bytes(), before)
        native_shadow.configure(self.root, False, self.path)
        self.assertEqual(json.loads(self.path.read_text()), original)
        self.assertFalse(native_shadow.configure(self.root, False, self.path)['changed'])

    def test_manually_modified_owned_hook_is_not_overwritten(self):
        native_shadow.configure(self.root, True, self.path)
        document = json.loads(self.path.read_text())
        document['hooks']['UserPromptSubmit'][0]['hooks'][0]['command'] = 'my command'
        self.path.write_text(json.dumps(document))
        before = self.path.read_bytes()
        for enabled in (True, False):
            with self.assertRaisesRegex(ValueError, 'manually modified'):
                native_shadow.configure(self.root, enabled, self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_manually_changed_label_is_detected_by_owned_command(self):
        native_shadow.configure(self.root, True, self.path)
        document = json.loads(self.path.read_text())
        document['hooks']['UserPromptSubmit'][0]['hooks'][0]['statusMessage'] = 'personal label'
        self.path.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, 'manually modified'):
            native_shadow.configure(self.root, True, self.path)

    def test_only_sanitized_dossier_reaches_router_no_execution_changes(self):
        router = mock.Mock()
        router.decide.return_value = {'proposed_model': 'gpt-6-luna', 'proposed_effort': 'low', 'reason': 'jev', 'jev_ms': 12}
        event = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'private-session',
                 'model': 'gpt-6.1-sol', 'prompt': 'Please explain what this code does briefly. /Users/private/file.py\n```python\npassword="private-secret"\n```'}
        original = dict(event)
        record = native_shadow.observe(self.root, event, router)
        payload = router.decide.call_args.args[0]
        self.assertEqual(payload['model'], 'effortlane-shadow')
        self.assertNotIn('private-secret', json.dumps(payload))
        self.assertNotIn('/Users/private', json.dumps(payload))
        self.assertEqual(router.decide.call_args.kwargs['mode_override'], 'shadow')
        self.assertEqual(record['actual_model'], 'gpt-6.1-sol')
        self.assertEqual(record['actual_effort'], 'unknown')
        self.assertNotIn('private-session', json.dumps(record))
        self.assertEqual(event, original)
        self.assertEqual(record['usage_scope'], 'proposal_only')

    def test_privacy_rejection_happens_before_router_or_key_access(self):
        with mock.patch.object(native_shadow, 'Router') as router:
            for prompt in ('<pasted_content>full repository</pasted_content>', '-----BEGIN PRIVATE KEY-----', 'x'*30_001):
                self.assertIsNone(native_shadow.observe(self.root, {'hook_event_name': 'UserPromptSubmit', 'prompt': prompt}))
            router.assert_not_called()

    def test_bridge_and_disabled_hooks_never_route(self):
        event = {'hook_event_name': 'UserPromptSubmit', 'prompt': 'Please explain this task in simple words'}
        with mock.patch.object(native_shadow, 'Router') as router:
            with mock.patch.dict(os.environ, {'EFFORTLANE_ROUTED': '1'}):
                self.assertIsNone(native_shadow.observe(self.root, event))
            (self.root / 'native-shadow-hook.json').write_text('{"enabled":false}')
            self.assertIsNone(native_shadow.observe(self.root, event))
            router.assert_not_called()

    def test_hook_is_silent_and_fails_open(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), mock.patch.object(native_shadow, 'observe', side_effect=TimeoutError):
            self.assertEqual(native_shadow.hook(self.root, io.StringIO('{"hook_event_name":"UserPromptSubmit"}')), 0)
            self.assertEqual(native_shadow.hook(self.root, io.StringIO('not JSON')), 0)
        self.assertEqual(out.getvalue(), '')
        self.assertEqual(err.getvalue(), '')

    def test_native_hook_definition_is_async_and_has_no_trust_bypass(self):
        handler = native_shadow.definition(self.root)['hooks'][0]
        self.assertTrue(handler['async'])
        self.assertEqual(handler['timeout'], 4)
        self.assertNotIn('bypass', handler['command'])
        self.assertNotIn('--model', handler['command'])

    def test_report_does_not_claim_usage_or_savings(self):
        (self.root / 'state').mkdir()
        rows = [{'event': 'route', 'model': 'hypothetical-sol'},
                {'event': 'native_shadow_proposal', 'actual_model': 'gpt-6.1-sol', 'proposed_model': 'gpt-6-luna', 'proposed_effort': 'low'}]
        (self.root / 'state/native-shadow-routing.jsonl').write_text('\n'.join(map(json.dumps, rows)))
        report = native_shadow.report(self.root)
        self.assertEqual(report['observations'], 1)
        self.assertEqual(report['savings'], 'not measured')
        self.assertEqual(report['proposed_efforts'], {'low': 1})


if __name__ == '__main__':
    unittest.main()
