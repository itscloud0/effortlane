import json
import hashlib
import io
from contextlib import redirect_stderr
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import core
import rpc_adapter


class Router:
    def __init__(self):
        self.calls = []
        self.usage_records = []
        self.subscription_records = []
        self.mode = 'auto'
        self.model = 'gpt-6-luna'

    def _catalog(self):
        return {'models': [{'slug': 'gpt-6-sol', 'visibility': 'list', 'supported_reasoning_levels': [{'effort': 'medium'}]}]}

    def _config(self):
        return {'mode': self.mode}

    def decide(self, payload, **kwargs):
        self.calls.append((payload, kwargs))
        return {'model': self.model, 'effort': 'low', 'mode': 'auto', 'reason': 'jev'}

    def record_subscription(self, *args):
        self.subscription_records.append(args)

    def record_usage(self, *args, **kwargs):
        self.usage_records.append((args, kwargs))


def request(method, params, rid=1):
    return (json.dumps({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params}) + '\n').encode()


def response(rid, result):
    return (json.dumps({'jsonrpc': '2.0', 'id': rid, 'result': result}) + '\n').encode()


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.router = Router()
        self.adapter = rpc_adapter.Adapter(self.root, router=self.router)

    def tearDown(self):
        self.tmp.cleanup()

    def test_native_server_uses_concrete_catalog_even_with_native_default(self):
        config = self.root / 'codex.toml'
        config.write_text('model = "gpt-6.1-sol"\n')
        (self.root / 'manifest.json').write_text(json.dumps({'config_path': str(config)}))
        (self.root / 'config.json').write_text(json.dumps({'fallback_model': 'gpt-6.1-sol'}))
        native = self.root / 'native-models.json'
        native.write_text(json.dumps({'models': [{'slug': 'gpt-6.1-sol', 'visibility': 'list'}]}))
        command = ['app-server', '--listen', 'stdio://']
        safe = rpc_adapter.native_server_command(self.root, command)
        self.assertIn('model_catalog_json=' + json.dumps(str(native)), safe)
        self.assertNotIn('model="effortlane-shadow"', safe)
        self.assertEqual(command, ['app-server', '--listen', 'stdio://'])
        manual = rpc_adapter.native_server_command(self.root, ['-c', 'model="gpt-6-luna"', *command])
        self.assertIn('model="gpt-6-luna"', manual)
        alias = rpc_adapter.native_server_command(self.root, ['-c', 'model="effortlane-shadow"', *command])
        self.assertIn('model="gpt-6.1-sol"', alias)
        custom = ['-c', 'profile="custom"', *command]
        self.assertEqual(rpc_adapter.native_server_command(self.root, custom), custom)

    def test_model_picker_aliases_are_owned_by_adapter_not_native(self):
        native = {'id': 'gpt-6-sol', 'model': 'gpt-6-sol', 'displayName': 'Sol',
                  'hidden': False, 'isDefault': True, 'defaultReasoningEffort': 'medium',
                  'supportedReasoningEfforts': [{'reasoningEffort': 'medium'}, {'reasoningEffort': 'high'}],
                  'inputModalities': ['text', 'image']}
        self.adapter.client(request('model/list', {}, 101))
        raw = response(101, {'data': [native], 'nextCursor': None})
        result = json.loads(self.adapter.server(raw))['result']
        self.assertEqual(result['data'][0], native)
        self.assertEqual([m['model'] for m in result['data'][1:]], ['effortlane-auto', 'effortlane-shadow'])
        self.assertEqual(result['data'][1]['displayName'], 'Effortlane Auto')
        self.assertFalse(result['data'][1]['isDefault'])
        self.assertEqual(result['data'][1]['supportedReasoningEfforts'], [{'reasoningEffort': 'medium'}])
        self.assertEqual(result['data'][2]['supportedReasoningEfforts'], native['supportedReasoningEfforts'])
        self.assertEqual(json.loads(raw)['result']['data'], [native])
        self.adapter.client(request('model/list', {'cursor': 'next'}, 102))
        other = response(102, {'data': [], 'nextCursor': None})
        self.assertEqual(self.adapter.server(other), other)
        self.adapter.client(request('model/list', {}, 104))
        repeated = json.loads(self.adapter.server(response(104, result)))['result']
        self.assertEqual(len(repeated['data']), 3)
        for rid, extra in ((105, {}), (106, {'params': None})):
            self.adapter.client((json.dumps({'id': rid, 'method': 'model/list', **extra}) + '\n').encode())
            default = json.loads(self.adapter.server(response(rid, {'data': [native], 'nextCursor': None})))
            self.assertEqual(len(default['result']['data']), 3)
        self.router.mode = 'off'
        self.adapter.client(request('model/list', {}, 103))
        disabled = response(103, {'data': [native], 'nextCursor': None})
        self.assertEqual(self.adapter.server(disabled), disabled)

    def test_missing_model_uses_launch_intent_and_preserves_local_images(self):
        cli = rpc_adapter.Adapter(self.root, router=self.router, client='cli', initial_alias='effortlane-shadow')
        started = json.loads(cli.client(request('thread/start', {}, 90)))
        self.assertEqual(started['params']['model'], 'gpt-6-sol')
        cli.server(response(90, {'thread': {'id': 'images'}, 'model': 'gpt-6-sol'}))
        items = [{'type': 'text', 'text': 'Inspect images'},
                 *[{'type': 'localImage', 'path': '/tmp/test-image-'+str(i)+'.png'} for i in range(4)]]
        sent = json.loads(cli.client(request('turn/start', {'threadId': 'images', 'input': items, 'effort': 'high'}, 91)))
        self.assertNotIn(sent['params']['model'], core.ALIASES)
        self.assertEqual(sent['params']['input'], items)
        self.assertEqual(self.router.calls[-1][0]['shadow_executor_effort'], 'high')

    def test_global_alias_is_adapter_intent_not_native_server_default(self):
        config = self.root / 'codex.toml'
        config.write_text('model = "effortlane-shadow"\n')
        (self.root / 'manifest.json').write_text(json.dumps({'config_path': str(config)}))
        (self.root / 'config.json').write_text(json.dumps({'fallback_model': 'gpt-6.1-sol'}))
        command = ['-c', 'model_catalog_json=' + json.dumps(str(self.root / 'models.json')), 'app-server']
        safe = rpc_adapter.native_server_command(self.root, command)
        self.assertEqual(safe[-3:], ['-c', 'model="gpt-6.1-sol"', 'app-server'])
        self.assertEqual(command, ['-c', 'model_catalog_json=' + json.dumps(str(self.root / 'models.json')), 'app-server'])
        adapter = rpc_adapter.Adapter(self.root, router=self.router)
        self.assertEqual(json.loads(adapter.client(request('thread/start', {})))['params']['model'], 'gpt-6-sol')
        concrete = ['-c', 'model="gpt-6-luna"', 'app-server']
        self.assertEqual(rpc_adapter.native_server_command(self.root, concrete), concrete)
        native_url = ['-c', 'openai_base_url="https://chatgpt.com/backend-api/codex"', 'app-server']
        self.assertIn('model="gpt-6.1-sol"', rpc_adapter.native_server_command(self.root, native_url))
        custom_url = ['-c', 'openai_base_url="https://custom.example"', 'app-server']
        self.assertEqual(rpc_adapter.native_server_command(self.root, custom_url), custom_url)
        profile = ['-c', 'profile="custom"', 'app-server']
        self.assertEqual(rpc_adapter.native_server_command(self.root, profile), profile)
        config.write_text('model = "effortlane-shadow"\nmodel_provider = "custom"\n')
        self.assertEqual(rpc_adapter.native_server_command(self.root, command), command)

    def test_config_alias_cannot_override_routed_native_model(self):
        sent = json.loads(self.adapter.client(request('turn/start', {
            'threadId': 'config-shadow', 'model': 'effortlane-shadow', 'effort': 'high',
            'config': {'model': 'effortlane-shadow', 'unrelated': 'preserve'},
            'input': [{'type': 'text', 'text': 'test'}, {'type': 'image', 'url': 'test-only'}]})))
        self.assertEqual(sent['params']['config']['model'], sent['params']['model'])
        self.assertNotIn(sent['params']['model'], core.ALIASES)
        self.assertEqual(sent['params']['config']['unrelated'], 'preserve')
        self.assertEqual(sent['params']['input'][1], {'type': 'image', 'url': 'test-only'})

    def test_state_failure_fails_open_without_leaking_alias(self):
        for method in ('thread/start', 'thread/resume', 'turn/start'):
            with self.subTest(method=method), mock.patch.object(self.adapter.store, 'get', side_effect=OSError('test-only')):
                sent = json.loads(self.adapter.client(request(method, {
                    'threadId': 'broken', 'model': 'effortlane-shadow', 'effort': 'high',
                    'config': {'model': 'effortlane-shadow'},
                    'collaborationMode': {'settings': {'model': 'effortlane-shadow', 'reasoning_effort': 'high'}},
                    'input': [{'type': 'text', 'text': 'preserve'}]})))
                self.assertEqual(sent['params']['model'], 'gpt-6-sol')
                self.assertEqual(sent['params']['config']['model'], 'gpt-6-sol')
                self.assertEqual(sent['params']['collaborationMode']['settings']['model'], 'gpt-6-sol')
                self.assertEqual(sent['params']['effort'], 'high')
                self.assertEqual(sent['params']['input'], [{'type': 'text', 'text': 'preserve'}])

    def test_cli_state_failure_does_not_print_over_tui(self):
        cli = rpc_adapter.Adapter(self.root, router=self.router, client='cli')
        output = io.StringIO()
        with mock.patch.object(cli.store, 'get', side_effect=OSError('test-only')), redirect_stderr(output):
            sent = json.loads(cli.client(request('turn/start', {'threadId': 'broken', 'model': 'effortlane-shadow'})))
        self.assertEqual(sent['params']['model'], 'gpt-6-sol')
        self.assertEqual(output.getvalue(), '')

    def test_native_guard_preserves_concrete_manual_model(self):
        raw = request('turn/start', {'model': 'effortlane-shadow', 'config': {'model': 'gpt-6-astra'}})
        guarded = json.loads(self.adapter._native_model_guard(raw))
        self.assertEqual(guarded['params']['model'], 'gpt-6-astra')
        self.assertEqual(guarded['params']['config']['model'], 'gpt-6-astra')
        self.assertEqual(self.adapter._native_model_guard(request('model/list', {})), request('model/list', {}))

    def test_auto_astra_proposal_is_clamped_but_concrete_astra_passes(self):
        self.router.model = 'gpt-6-astra'
        routed = json.loads(self.adapter.client(request('turn/start', {'threadId': 'auto', 'model': 'effortlane-auto',
            'input': [{'type': 'text', 'text': 'Plan an architecture change'}]})))
        self.assertEqual(routed['params']['model'], 'gpt-6-sol')
        manual = json.loads(self.adapter.client(request('turn/start', {'threadId': 'manual', 'model': 'gpt-6-astra',
            'input': [{'type': 'text', 'text': 'Plan an architecture change'}]}, 2)))
        self.assertEqual(manual['params']['model'], 'gpt-6-astra')

    def test_astra_allowlist_routes_without_dialog(self):
        self.router._config = lambda: {'mode': 'auto', 'auto_roles': ['luna', 'terra', 'sol', 'astra']}
        self.router.model = 'gpt-6-astra'
        routed = json.loads(self.adapter.client(request('turn/start', {'threadId': 'allowed', 'model': 'effortlane-auto',
            'input': [{'type': 'text', 'text': 'Review architecture'}]})))
        self.assertEqual(routed['params']['model'], 'gpt-6-astra')

    def test_native_model_picker_can_return_from_concrete_to_auto(self):
        self.adapter.client(request('thread/start', {'model': 'gpt-6-sol'}, 1))
        self.adapter.server(response(1, {'thread': {'id': 'picker'}, 'model': 'gpt-6-sol'}))
        changed = json.loads(self.adapter.client(request('thread/settings/update',
            {'threadId': 'picker', 'model': 'effortlane-auto'}, 2)))
        self.assertEqual(changed['params']['model'], 'gpt-6-sol')
        turn = json.loads(self.adapter.client(request('turn/start', {'threadId': 'picker',
            'model': 'effortlane-auto', 'input': [{'type': 'text', 'text': 'Fix typo'}]}, 3)))
        self.assertEqual((turn['params']['model'], turn['params']['effort']), ('gpt-6-luna', 'low'))

    def test_subscription_notifications_pass_through_and_deduplicate(self):
        raw = (json.dumps({"method": "account/rateLimits/updated", "params": {"rateLimits": {
            "planType": "pro", "primary": {"usedPercent": 12, "windowDurationMins": 10080, "resetsAt": 1900000000}}}}) + "\n").encode()
        self.assertEqual(self.adapter.server(raw), raw)
        self.assertEqual(self.adapter.server(raw), raw)
        self.assertEqual(len(self.router.subscription_records), 1)

    def test_turn_metrics_do_not_capture_tool_content_or_count_steering_twice(self):
        with mock.patch.object(rpc_adapter.time, "monotonic", return_value=10):
            self.adapter.client(request('turn/start', {'threadId': 't', 'model': 'effortlane-shadow',
                'input': [{'type': 'text', 'text': 'Explain the module'}]}))
        with mock.patch.object(rpc_adapter.time, "monotonic", return_value=11):
            self.adapter.client(request('turn/start', {'threadId': 't', 'model': 'effortlane-shadow',
                'input': [{'type': 'text', 'text': 'Steering'}]}))
            raw = (json.dumps({'method': 'item/agentMessage/delta', 'params': {'threadId': 't', 'delta': 'private'}})+'\n').encode()
            self.assertEqual(self.adapter.server(raw), raw)
            self.adapter.server((json.dumps({'method': 'item/started', 'params': {'threadId': 't',
                'item': {'type': 'commandExecution', 'command': 'private command'}}})+'\n').encode())
            self.adapter.server((json.dumps({'method': 'thread/compacted', 'params': {'threadId': 't'}})+'\n').encode())
        with mock.patch.object(rpc_adapter.time, "monotonic", return_value=12):
            self.adapter.server((json.dumps({'method': 'turn/completed', 'params': {'threadId': 't',
                'turn': {'id': 'turn-1', 'status': 'completed'}}})+'\n').encode())
        decision = self.router.usage_records[-1][0][0]
        self.assertEqual(decision['turn_duration_ms'], 2000)
        self.assertEqual(decision['first_response_ms'], 1000)
        self.assertEqual(decision['tool_calls'], 1)
        self.assertEqual(decision['compactions'], 1)
        self.assertEqual(decision['usage_scope'], 'last_model_call')
        self.assertNotIn('private', json.dumps(decision))

    def test_passthrough(self):
        for raw in (b'{ "jsonrpc":"2.0", "method":"other", "params":{"secret":"x"} }\n',
                    b'{"jsonrpc":"2.0","id":55,"method":"approval","params":{"foo":1}}\n',
                    b'{"jsonrpc":"2.0","id":7,"error":{"code":-1,"message":"opaque"}}\n',
                    b'bad json\n', b'[1,2]\n'):
            self.assertEqual(self.adapter.client(raw), raw)
            self.assertEqual(self.adapter.server(raw), raw)
        self.assertFalse(self.router.calls)

    def test_start_turn_usage_and_privacy(self):
        sent = json.loads(self.adapter.client(request('thread/start', {'model': 'effortlane-auto', 'cwd': '/tmp'})))
        self.assertEqual(sent['params'], {'model': 'gpt-6-sol', 'cwd': '/tmp'})
        returned = json.loads(self.adapter.server(response(1, {'thread': {'id': 'thread-1', 'model': 'gpt-6-sol'}, 'model': 'gpt-6-sol'})))
        self.assertEqual(returned['result']['model'], 'effortlane-auto')
        self.assertEqual(returned['result']['thread']['model'], 'effortlane-auto')
        input_items = [{'type': 'text', 'text': 'Fix a simple typo'}, {'type': 'image', 'data': 'secret-image'}]
        sent = json.loads(self.adapter.client(request('turn/start', {'threadId': 'thread-1', 'model': 'effortlane-auto', 'input': input_items}, 2)))
        self.assertEqual((sent['params']['model'], sent['params']['effort']), ('gpt-6-luna', 'low'))
        self.assertEqual(sent['params']['input'], input_items)
        self.assertEqual(self.router.calls[0][1], {'client': 'desktop', 'session_id': 'thread-1', 'native_selection': True, 'mode_override': 'auto'})
        self.assertNotIn('secret-image', json.dumps(self.router.calls[0][0]))
        usage = {'jsonrpc': '2.0', 'method': 'thread/tokenUsage/updated', 'params': {'threadId': 'thread-1', 'tokenUsage': {'last': {'inputTokens': 9000}}}}
        self.adapter.server((json.dumps(usage) + '\n').encode())
        state = self.root / 'state/desktop-intent.json'
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)
        self.assertNotIn('thread-1', state.read_text())
        self.assertNotIn('typo', state.read_text())
        self.assertEqual(self.adapter.store.get('thread-1')['context'], 9000)
        cached_usage = {'jsonrpc': '2.0', 'method': 'thread/tokenUsage/updated', 'params': {
            'threadId': 'thread-1', 'tokenUsage': {'last': {'inputTokens': 10000,
                                                          'cachedInputTokens': 2500, 'outputTokens': 100}}}}
        self.adapter.server((json.dumps(cached_usage) + '\n').encode())
        self.adapter._route('effortlane-auto', {'input': [{'type': 'text', 'text': 'Fix the next typo'}]},
                            'thread-1', self.adapter.store.get('thread-1'))
        self.assertEqual(self.router.calls[-1][0]['cached_input_pct'], 25)
        self.assertEqual(self.router.calls[-1][0]['cache_state'], 'hot')
        self.adapter.last_cache_pct['thread-1'] = (25, rpc_adapter.time.monotonic() - 601)
        self.adapter._route('effortlane-auto', {'input': [{'type': 'text', 'text': 'Fix another typo'}]},
                            'thread-1', self.adapter.store.get('thread-1'))
        self.assertNotIn('cached_input_pct', self.router.calls[-1][0])
        settings = {'jsonrpc': '2.0', 'method': 'thread/settings/updated', 'params': {'threadId': 'thread-1', 'threadSettings': {'model': 'gpt-6-luna', 'effort': 'low'}}}
        self.assertEqual(json.loads(self.adapter.server((json.dumps(settings)+'\n').encode()))['params']['threadSettings']['model'], 'effortlane-auto')

    def test_cache_projection_uses_exact_native_per_call_counters(self):
        self.adapter.actual['sample-thread'] = ('gpt-6-sol', 'medium')
        update = {'jsonrpc': '2.0', 'method': 'thread/tokenUsage/updated', 'params': {
            'threadId': 'sample-thread', 'tokenUsage': {'last': {
                'inputTokens': 10000, 'cachedInputTokens': 9500, 'outputTokens': 120}}}}
        raw = (json.dumps(update) + '\n').encode()
        self.assertEqual(self.adapter.server(raw), raw)
        self.adapter._route('effortlane-auto', {'input': [{'type': 'text', 'text': 'Rename a variable'}]},
                            'sample-thread', None)
        sent = self.router.calls[-1][0]
        self.assertEqual((sent['cache_sample_model'], sent['cache_sample_input'],
                          sent['cache_sample_cached'], sent['cache_sample_output']),
                         ('gpt-6-sol', 10000, 9500, 120))
        self.assertLessEqual(sent['cache_sample_age_s'], 1)
        update['params']['tokenUsage']['last'] = {'inputTokens': 9000}
        self.adapter.server((json.dumps(update) + '\n').encode())
        self.adapter._route('effortlane-auto', {'input': [{'type': 'text', 'text': 'Rename another variable'}]},
                            'sample-thread', None)
        self.assertNotIn('cache_sample_input', self.router.calls[-1][0])

    def test_thread_metadata_keeps_alias_for_desktop_effort_picker(self):
        self.adapter.store.update('t', alias='effortlane-auto', actual='gpt-6-sol', effort='medium')
        started = {'jsonrpc': '2.0', 'method': 'thread/started', 'params': {'thread': {'id': 't', 'model': 'gpt-6-sol'}}}
        self.assertEqual(json.loads(self.adapter.server((json.dumps(started)+'\n').encode()))['params']['thread']['model'], 'effortlane-auto')
        self.assertEqual(self.adapter.client(request('thread/read', {'threadId': 't'}, 5)), request('thread/read', {'threadId': 't'}, 5))
        read = json.loads(self.adapter.server(response(5, {'thread': {'id': 't', 'model': 'gpt-6-sol'}})))
        self.assertEqual(read['result']['thread']['model'], 'effortlane-auto')
        self.adapter.client(request('thread/list', {'limit': 10}, 6))
        listed = json.loads(self.adapter.server(response(6, {'data': [{'id': 't', 'model': 'gpt-6-sol'},
                                                                        {'id': 'manual', 'model': 'gpt-6-astra'}]})))
        self.assertEqual([x['model'] for x in listed['result']['data']], ['effortlane-auto', 'gpt-6-astra'])
        settings = {'jsonrpc': '2.0', 'method': 'thread/settings/updated', 'params': {
            'threadId': 't', 'threadSettings': {'model': 'gpt-6-sol', 'effort': 'high',
            'collaborationMode': {'settings': {'model': 'gpt-6-sol', 'reasoning_effort': 'high'}}}}}
        returned = json.loads(self.adapter.server((json.dumps(settings)+'\n').encode()))['params']['threadSettings']
        self.assertEqual(returned['model'], 'effortlane-auto')
        self.assertEqual(returned['collaborationMode']['settings']['model'], 'effortlane-auto')
        self.adapter.store.update('t', clear=True)
        self.adapter.client(request('thread/read', {'threadId': 't'}, 7))
        manual = json.loads(self.adapter.server(response(7, {'thread': {'id': 't', 'model': 'gpt-6-sol'}})))
        self.assertEqual(manual['result']['thread']['model'], 'gpt-6-sol')

    def test_auto_ignores_picker_effort_and_clears_legacy_override(self):
        self.router._catalog = lambda: {'models': [
            {'slug': 'gpt-6-luna', 'visibility': 'list', 'supported_reasoning_levels':
             [{'effort': x} for x in ('low', 'medium', 'high', 'max')]},
            {'slug': 'gpt-6-sol', 'visibility': 'list', 'supported_reasoning_levels':
             [{'effort': x} for x in ('low', 'medium', 'high', 'max', 'ultra')]}]}
        self.adapter.store.update('t', alias='effortlane-auto', actual='gpt-6-sol', effort='medium', effort_override='max')
        changed = json.loads(self.adapter.client(request('thread/settings/update', {
            'threadId': 't', 'model': 'effortlane-auto', 'effort': 'high'}, 11)))
        self.assertEqual((changed['params']['model'], changed['params']['effort']), ('gpt-6-sol', 'high'))
        self.assertNotIn('effort_override', self.adapter.store.get('t'))
        turn = json.loads(self.adapter.client(request('turn/start', {'threadId': 't', 'model': 'effortlane-auto',
            'input': [{'type': 'text', 'text': 'simple task'}]}, 12)))
        self.assertEqual((turn['params']['model'], turn['params']['effort']), ('gpt-6-luna', 'low'))
        self.assertNotIn('requested_effort', self.router.calls[-1][0])
        self.adapter.active.discard('t')
        self.adapter.client(request('thread/settings/update', {'threadId': 't', 'model': 'effortlane-auto', 'effort': 'ultra'}, 13))
        turn = json.loads(self.adapter.client(request('turn/start', {'threadId': 't', 'model': 'effortlane-auto',
            'input': [{'type': 'text', 'text': 'simple task'}]}, 14)))
        self.assertEqual((turn['params']['model'], turn['params']['effort']), ('gpt-6-luna', 'low'))
        self.assertNotIn('requested_effort', self.router.calls[-1][0])
        self.adapter.client(request('thread/settings/update', {'threadId': 't', 'model': 'effortlane-auto', 'effort': None}, 15))
        self.assertNotIn('effort_override', self.adapter.store.get('t'))

    def test_initial_nondefault_effort_does_not_override_auto(self):
        raw = request('thread/start', {'model': 'effortlane-auto', 'config': {'model_reasoning_effort': 'high'}}, 10)
        self.adapter.client(raw)
        self.adapter.server(response(10, {'thread': {'id': 't', 'model': 'gpt-6-sol'},
                                          'model': 'gpt-6-sol', 'reasoningEffort': 'high'}))
        self.assertNotIn('effort_override', self.adapter.store.get('t'))

    def test_shadow_picker_effort_controls_sol_but_not_jev_and_survives_resume(self):
        (self.root / 'config.json').write_text(json.dumps({'mode': 'auto', 'shadow_policy': 'completion_v4'}))
        (self.root / 'catalog.json').write_text(json.dumps({'models': [
            {'slug': 'gpt-6-luna', 'visibility': 'list', 'supported_in_api': True,
             'supported_reasoning_levels': [{'effort': x} for x in ('low', 'medium', 'high')]},
            {'slug': 'gpt-6.1-sol', 'visibility': 'list', 'supported_in_api': True,
             'supported_reasoning_levels': [{'effort': x} for x in ('low', 'medium', 'high', 'xhigh', 'max', 'ultra')]}]}))
        bodies = []
        def jev(body, timeout, key_file):
            bodies.append(body)
            return {'answers': {'work_shape': {'choice': 'mechanical'}, 'effort': {'choice': 'low'}}}
        router = core.Router(self.root / 'config.json', self.root / 'catalog.json',
                             self.root / 'leases.json', self.root / 'telemetry.jsonl', jev)
        adapter = rpc_adapter.Adapter(self.root, router=router)
        started = json.loads(adapter.client(request('thread/start', {'model': 'effortlane-shadow', 'effort': 'high'}, 30)))
        self.assertEqual(started['params']['model'], 'gpt-6.1-sol')
        adapter.server(response(30, {'thread': {'id': 'shadow-thread'}, 'model': 'gpt-6.1-sol',
                                     'reasoningEffort': 'high'}))
        first = json.loads(adapter.client(request('turn/start', {'threadId': 'shadow-thread', 'model': 'effortlane-shadow',
            'input': [{'type': 'text', 'text': 'Rename a variable'}]}, 31)))
        self.assertEqual((first['params']['model'], first['params']['effort']), ('gpt-6.1-sol', 'high'))
        self.assertIn('effort', bodies[-1]['questions'])
        self.assertNotIn('requested_effort', bodies[-1]['state'])
        receipt = json.loads((self.root / 'telemetry.jsonl').read_text().splitlines()[-1])
        self.assertEqual((receipt['model'], receipt['effort'], receipt['proposed_model'], receipt['proposed_effort']),
                         ('gpt-6.1-sol', 'high', 'gpt-6-luna', 'low'))
        adapter.server((json.dumps({'method': 'turn/completed', 'params': {
            'threadId': 'shadow-thread', 'turn': {'id': 'turn-1', 'status': 'completed'}}}) + '\n').encode())
        adapter.client(request('thread/settings/update', {'threadId': 'shadow-thread', 'model': 'effortlane-shadow',
                                                         'effort': 'xhigh'}, 32))
        self.assertEqual(adapter.store.get('shadow-thread')['effort_override'], 'xhigh')
        resumed = rpc_adapter.Adapter(self.root, router=router)
        resumed.client(request('thread/resume', {'threadId': 'shadow-thread', 'model': 'effortlane-shadow'}, 33))
        resumed.server(response(33, {'thread': {'id': 'shadow-thread'}, 'model': 'gpt-6.1-sol',
                                     'reasoningEffort': 'medium'}))
        second = json.loads(resumed.client(request('turn/start', {'threadId': 'shadow-thread',
            'input': [{'type': 'text', 'text': 'Rename another variable'}]}, 34)))
        self.assertEqual((second['params']['model'], second['params']['effort']), ('gpt-6.1-sol', 'xhigh'))

    def test_auto_receives_last_concrete_model_for_cache_continuity(self):
        self.adapter.client(request('turn/start', {'threadId': 't', 'model': 'gpt-6-sol',
            'effort': 'max', 'input': [{'type': 'text', 'text': 'Manual work'}]}, 20))
        self.adapter.client(request('turn/start', {'threadId': 't', 'model': 'effortlane-auto',
            'input': [{'type': 'text', 'text': 'Continue'}]}, 21))
        self.assertEqual(self.router.calls[-1][0]['current_model'], 'gpt-6-sol')
        self.assertNotIn('requested_effort', self.router.calls[-1][0])

    def test_manual_and_collaboration_precedence(self):
        self.adapter.store.update('t', alias='effortlane-auto')
        raw = request('turn/start', {'threadId': 't', 'model': 'gpt-6-astra', 'input': []})
        self.assertEqual(self.adapter.client(raw), raw)
        self.assertIsNone(self.adapter.store.get('t'))
        self.adapter.store.update('t', alias='effortlane-auto')
        raw = request('turn/start', {'threadId': 't', 'model': 'effortlane-auto', 'collaborationMode': {'mode': 'plan', 'settings': {'model': 'gpt-6-astra', 'reasoning_effort': 'high'}}, 'input': []})
        sent = json.loads(self.adapter.client(raw))
        self.assertEqual(sent['params']['model'], 'gpt-6-astra')
        self.assertIsNone(self.adapter.store.get('t'))
        self.adapter.store.update('t', alias='effortlane-auto')
        raw = request('turn/start', {'threadId': 't', 'model': 'effortlane-auto', 'collaborationMode': {'mode': 'plan', 'settings': {'model': 'effortlane-auto', 'developerInstructions': 'keep'}}, 'input': [{'type': 'text', 'text': 'test'}]})
        sent = json.loads(self.adapter.client(raw))
        self.assertEqual(sent['params']['collaborationMode']['settings']['model'], 'gpt-6-luna')
        self.assertEqual(sent['params']['collaborationMode']['settings']['reasoning_effort'], 'low')
        self.assertEqual(sent['params']['collaborationMode']['settings']['developerInstructions'], 'keep')

    def test_native_usage_notification_is_recorded_without_prompt(self):
        self.adapter.client(request('turn/start', {'threadId': 't', 'model': 'gpt-6-sol', 'effort': 'medium',
                                                   'input': [{'type': 'text', 'text': 'private prompt'}]}))
        usage = {'jsonrpc': '2.0', 'method': 'thread/tokenUsage/updated', 'params': {
            'threadId': 't', 'turnId': 'turn-1', 'tokenUsage': {'last': {
                'inputTokens': 100, 'cachedInputTokens': 40, 'outputTokens': 7}}}}
        self.adapter.server((json.dumps(usage) + '\n').encode())
        complete = {'jsonrpc': '2.0', 'method': 'turn/completed', 'params': {
            'threadId': 't', 'turn': {'id': 'turn-1', 'status': 'completed'}}}
        self.adapter.server((json.dumps(complete) + '\n').encode())
        (decision, observed, status), _ = self.router.usage_records[-1]
        self.assertEqual((decision['model'], decision['effort'], status), ('gpt-6-sol', 'medium', 'ok'))
        self.assertEqual(decision['session'], hashlib.sha256(b't').hexdigest()[:24])
        self.assertEqual(decision['turn_hash'], hashlib.sha256(b'turn-1').hexdigest()[:24])
        self.assertEqual(observed['input_tokens'], 100)
        self.assertEqual(observed['input_tokens_details']['cached_tokens'], 40)
        self.assertNotIn('private prompt', json.dumps((decision, observed)))

    def test_auto_route_id_links_only_its_completed_turn(self):
        self.adapter.client(request('turn/start', {'threadId': 't', 'model': 'effortlane-auto',
                                                   'input': [{'type': 'text', 'text': 'private prompt'}]}))
        route = self.router.usage_records[-1][0][0]
        self.assertEqual(len(route['route_id']), 24)
        self.assertNotIn('private prompt', json.dumps(route))
        completed = {'jsonrpc': '2.0', 'method': 'turn/completed', 'params': {
            'threadId': 't', 'turn': {'id': 'turn-1', 'status': 'completed'}}}
        self.adapter.server((json.dumps(completed) + '\n').encode())
        usage = self.router.usage_records[-1][0][0]
        self.assertEqual(usage['route_id'], route['route_id'])
        self.assertNotIn('t', usage['route_id'])
        self.adapter.client(request('turn/start', {'threadId': 't', 'model': 'gpt-6-sol',
                                                   'input': [{'type': 'text', 'text': 'manual'}]}, 2))
        completed['params']['turn']['id'] = 'turn-2'
        self.adapter.server((json.dumps(completed) + '\n').encode())
        self.assertEqual(self.router.usage_records[-1][0][0]['route_id'], '')

    @staticmethod
    def usage_event(turn, total, last=None):
        def counters(values):
            inp, cached, output, reasoning = values
            return {'inputTokens': inp, 'cachedInputTokens': cached, 'outputTokens': output,
                    'reasoningOutputTokens': reasoning, 'totalTokens': inp + output}
        return (json.dumps({'method': 'thread/tokenUsage/updated', 'params': {
            'threadId': 't', 'turnId': turn,
            'tokenUsage': {'total': counters(total), 'last': counters(last or total)}}})+'\n').encode()

    def begin_measured_turn(self, turn, fresh=False):
        if fresh:
            self.adapter.client(request('thread/start', {'model': 'effortlane-shadow'}, 80))
            self.adapter.server(response(80, {'thread': {'id': 't'}, 'model': 'gpt-6-sol'}))
        self.adapter.client(request('turn/start', {'threadId': 't', 'model': 'effortlane-shadow'}, 81))
        self.adapter.server((json.dumps({'method': 'turn/started', 'params': {
            'threadId': 't', 'turn': {'id': turn}}})+'\n').encode())

    def complete_measured_turn(self, turn, status='completed'):
        raw = (json.dumps({'method': 'turn/completed', 'params': {
            'threadId': 't', 'turn': {'id': turn, 'status': status}}})+'\n').encode()
        self.assertEqual(self.adapter.server(raw), raw)
        return self.router.usage_records[-1][0]

    def test_full_turn_counts_multiple_calls_once_and_preserves_context_size(self):
        self.begin_measured_turn('a', fresh=True)
        first = self.usage_event('a', (100, 40, 10, 5))
        self.assertEqual(self.adapter.server(first), first)
        self.adapter.server(first)  # duplicate cumulative snapshot, not another call
        self.adapter.server(self.usage_event('a', (250, 160, 30, 17), (150, 120, 20, 12)))
        decision, usage, status = self.complete_measured_turn('a')
        self.assertEqual((decision['usage_scope'], decision['usage_coverage_reason']), ('turn_total', 'cumulative_delta'))
        self.assertEqual(usage['input_tokens'], 250)
        self.assertEqual(usage['output_tokens'], 30)
        self.assertEqual(usage['input_tokens_details']['cached_tokens'], 160)
        self.assertEqual(usage['output_tokens_details']['reasoning_tokens'], 17)
        self.assertEqual(self.adapter.last_context['t'], 150)  # context != cumulative input
        size = len(self.router.usage_records)
        self.complete_measured_turn('a')
        self.assertEqual(len(self.router.usage_records), size)
        self.begin_measured_turn('b')
        self.adapter.server(self.usage_event('a', (100, 40, 10, 5)))  # delayed old snapshot
        self.adapter.server(self.usage_event('b', (450, 340, 55, 32), (200, 180, 25, 15)))
        decision, usage, _ = self.complete_measured_turn('b')
        self.assertEqual((decision['usage_scope'], usage['input_tokens']), ('turn_total', 200))

    def test_resume_without_baseline_is_partial_then_next_turn_has_full_coverage(self):
        self.begin_measured_turn('a')
        self.adapter.server(self.usage_event('a', (50000, 40000, 1000, 500), (100, 40, 10, 5)))
        decision, usage, _ = self.complete_measured_turn('a')
        self.assertEqual((decision['usage_scope'], decision['usage_coverage_reason']), ('last_model_call', 'baseline_missing'))
        self.assertEqual(usage['input_tokens'], 100)
        self.begin_measured_turn('b')
        self.adapter.server(self.usage_event('b', (50200, 40150, 1030, 520), (200, 150, 30, 20)))
        decision, usage, _ = self.complete_measured_turn('b', 'interrupted')
        self.assertEqual((decision['usage_scope'], usage['input_tokens']), ('turn_total', 200))

    def test_reset_or_compaction_never_claims_full_turn(self):
        for scenario in ('reset', 'compaction', 'malformed', 'missing'):
            with self.subTest(scenario=scenario):
                old_tmp = self.tmp
                self.setUp()
                try:
                    self.begin_measured_turn('a', fresh=True)
                    self.adapter.server(self.usage_event('a', (100, 40, 10, 5)))
                    if scenario == 'compaction':
                        self.adapter.server(b'{"method":"thread/compacted","params":{"threadId":"t"}}\n')
                    second = json.loads(self.usage_event('a', (50, 30, 5, 2) if scenario == 'reset' else (200, 100, 20, 9)))
                    if scenario == 'malformed':
                        second['params']['tokenUsage']['total']['totalTokens'] = 999999
                    if scenario == 'missing':
                        second['params']['tokenUsage'].pop('total')
                    self.adapter.server((json.dumps(second)+'\n').encode())
                    decision, _, _ = self.complete_measured_turn('a')
                    self.assertEqual(decision['usage_scope'], 'last_model_call')
                    reasons = {'reset': 'counter_reset', 'missing': 'total_missing',
                               'malformed': 'invalid_total', 'compaction': 'compacted'}
                    self.assertEqual(decision['usage_coverage_reason'], reasons[scenario])
                finally:
                    self.tearDown()
                    self.tmp = old_tmp

    def test_no_usage_is_missing_not_zero_and_turn_start_error_clears_window(self):
        self.begin_measured_turn('a', fresh=True)
        decision, usage, _ = self.complete_measured_turn('a')
        self.assertIsNone(usage)
        self.assertEqual(decision['usage_coverage_reason'], 'no_usage')
        self.begin_measured_turn('b')
        self.adapter.server(b'{"id":81,"error":{"code":-1,"message":"failed"}}\n')
        self.assertNotIn('t', self.adapter.usage_windows)
        self.assertNotIn('t', self.adapter.turn_started)

    def test_cli_client_routes_before_native_turn_with_cli_receipt(self):
        adapter = rpc_adapter.Adapter(self.root, router=self.router, client='cli')
        sent = json.loads(adapter.client(request('turn/start', {'threadId': 'cli-thread', 'model': 'effortlane-auto',
            'input': [{'type': 'text', 'text': 'Rename a local variable'}]})))
        self.assertEqual((sent['params']['model'], sent['params']['effort']), ('gpt-6-luna', 'low'))
        self.assertEqual(self.router.calls[-1][1]['client'], 'cli')
        receipt = self.router.usage_records[-1][0][0]
        self.assertEqual(len(receipt['route_id']), 24)
        adapter.server((json.dumps({'method': 'turn/completed', 'params': {
            'threadId': 'cli-thread', 'turn': {'id': 'turn-1', 'status': 'completed'}}}) + '\n').encode())
        completed = self.router.usage_records[-1][0][0]
        self.assertEqual(completed['client'], 'cli')
        self.assertEqual(completed['route_id'], receipt['route_id'])

    def test_cli_turn_falls_back_to_sol_when_router_fails(self):
        adapter = rpc_adapter.Adapter(self.root, router=self.router, client='cli')
        self.router.decide = lambda *args, **kwargs: (_ for _ in ()).throw(OSError('Jev unavailable'))
        sent = json.loads(adapter.client(request('turn/start', {'threadId': 'cli-fallback',
            'model': 'effortlane-auto', 'input': [{'type': 'text', 'text': 'Fix a bug'}]})))
        self.assertEqual((sent['params']['model'], sent['params']['effort']), ('gpt-6-sol', 'medium'))

    def test_desktop_weak_quality_signals_are_counts_only(self):
        self.adapter.store.update('t', alias='effortlane-auto', failed=True)
        self.adapter.client(request('turn/start', {'threadId': 't', 'model': 'effortlane-auto',
                                                   'input': [{'type': 'text', 'text': 'private prompt'}]}))
        failed_command = {'jsonrpc': '2.0', 'method': 'item/completed', 'params': {'threadId': 't',
            'item': {'type': 'commandExecution', 'exitCode': 1, 'aggregatedOutput': 'private source'}}}
        self.adapter.server((json.dumps(failed_command) + '\n').encode())
        completed = {'jsonrpc': '2.0', 'method': 'turn/completed', 'params': {
            'threadId': 't', 'turn': {'id': 'turn-1', 'status': 'completed'}}}
        self.adapter.server((json.dumps(completed) + '\n').encode())
        decision = self.router.usage_records[-1][0][0]
        self.assertEqual((decision['prior_failed'], decision['command_failures']), (True, 1))
        self.assertNotIn('private', json.dumps(decision))
        self.adapter.store.update('m', alias='effortlane-auto')
        self.adapter.client(request('turn/start', {'threadId': 'm', 'model': 'gpt-6-sol',
                                                   'input': [{'type': 'text', 'text': 'manual'}]}))
        self.adapter.server((json.dumps({**completed, 'params': {'threadId': 'm',
            'turn': {'id': 'turn-2', 'status': 'completed'}}}) + '\n').encode())
        self.assertTrue(self.router.usage_records[-1][0][0]['manual_override'])
        self.adapter.store.update('s', alias='effortlane-auto')
        self.adapter.client(request('thread/settings/update', {'threadId': 's', 'model': 'gpt-6-sol'}))
        self.adapter.client(request('turn/start', {'threadId': 's', 'model': 'gpt-6-sol',
                                                   'input': [{'type': 'text', 'text': 'continue'}]}))
        self.adapter.server((json.dumps({**completed, 'params': {'threadId': 's',
            'turn': {'id': 'turn-3', 'status': 'completed'}}}) + '\n').encode())
        self.assertTrue(self.router.usage_records[-1][0][0]['manual_override'])

    def test_legacy_alias_input_and_saved_intent_emit_canonical_ids(self):
        import time
        path = self.root / 'state/desktop-intent.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({rpc_adapter._key('legacy'): {
            'alias': 'jev-shadow', 'actual': 'gpt-6-sol', 'effort': 'high',
            'effort_override': 'high', 'context': 1000, 'updated': time.time()}}))
        self.assertEqual(self.adapter.store.get('legacy')['alias'], 'effortlane-shadow')
        sent = json.loads(self.adapter.client(request('thread/resume', {
            'threadId': 'legacy', 'model': 'jev-shadow'}, 91)))
        self.assertEqual(sent['params']['model'], 'gpt-6-sol')
        result = json.loads(self.adapter.server(response(91, {
            'thread': {'id': 'legacy'}, 'model': 'gpt-6-sol'})))['result']
        self.assertEqual(result['model'], 'effortlane-shadow')
        self.adapter.store.update('legacy', context=1200)
        self.assertEqual(json.loads(path.read_text())[rpc_adapter._key('legacy')]['alias'],
                         'effortlane-shadow')
        self.adapter.store.update('new', alias='jev-auto')
        self.assertEqual(self.adapter.store.get('new')['alias'], 'effortlane-auto')
        cli = rpc_adapter.Adapter(self.root, router=self.router, client='cli', initial_alias='jev-auto')
        self.assertEqual(cli.initial_alias, 'effortlane-auto')

    def test_resume_persistence_unknown_context_and_fork(self):
        self.adapter.store.update('t', alias='effortlane-auto', actual='gpt-6-astra', effort='high', conservative=True)
        other = rpc_adapter.Adapter(self.root, router=self.router)
        self.assertEqual(json.loads(other.client(request('thread/resume', {'threadId': 't', 'model': 'effortlane-auto'})))['params']['model'], 'gpt-6-sol')
        other.server(response(1, {'thread': {'id': 't'}, 'model': 'gpt-6-astra'}))
        sent = json.loads(other.client(request('turn/start', {'threadId': 't', 'input': [{'type': 'text', 'text': 'continue'}]})))
        self.assertEqual(sent['params']['model'], 'gpt-6-sol')
        other.store.update('unknown', alias='effortlane-auto')
        unknown_resume = json.loads(other.client(request('thread/resume', {'threadId': 'unknown', 'model': 'effortlane-auto'}, 3)))
        self.assertNotIn('model', unknown_resume['params'])
        other.server(response(3, {'thread': {'id': 'unknown'}, 'model': 'gpt-6-sol'}))
        sent = json.loads(other.client(request('turn/start', {'threadId': 'unknown', 'input': [{'type': 'text', 'text': 'continue'}]}, 4)))
        self.assertEqual(sent['params']['model'], 'gpt-6-sol')
        other.client(request('thread/fork', {'threadId': 'unknown'}, 5))
        other.server(response(5, {'thread': {'id': 'forked'}, 'model': 'gpt-6-sol'}))
        self.assertEqual(other.store.get('forked')['alias'], 'effortlane-auto')

    def test_cli_launch_alias_routes_resumed_concrete_model_and_manual_switch_wins(self):
        cli = rpc_adapter.Adapter(self.root, router=self.router, client='cli', initial_alias='effortlane-auto')
        sent = json.loads(cli.client(request('thread/resume', {'threadId': 'old', 'model': 'gpt-6-sol'}, 80)))
        self.assertNotIn('model', sent['params'])
        cli.server(response(80, {'thread': {'id': 'old'}, 'model': 'gpt-6-sol', 'reasoningEffort': 'medium'}))
        cli.store.update('old', context=1000)
        turn = json.loads(cli.client(request('turn/start', {'threadId': 'old', 'model': 'gpt-6-sol',
            'input': [{'type': 'text', 'text': 'Fix a typo'}]}, 81)))
        self.assertEqual((turn['params']['model'], turn['params']['effort']), ('gpt-6-luna', 'low'))
        self.assertEqual(len(self.router.calls), 1)
        cli.server((json.dumps({'jsonrpc': '2.0', 'method': 'turn/completed',
                                 'params': {'threadId': 'old', 'turn': {'id': 'turn-1', 'status': 'completed'}}})+'\n').encode())
        cli.client(request('thread/settings/update', {'threadId': 'old', 'model': 'gpt-6-sol'}, 82))
        manual = json.loads(cli.client(request('turn/start', {'threadId': 'old', 'model': 'gpt-6-sol',
            'input': [{'type': 'text', 'text': 'Keep Sol'}]}, 83)))
        self.assertEqual(manual['params']['model'], 'gpt-6-sol')
        self.assertEqual(len(self.router.calls), 1)

    def test_steer_failure_kill_switch_custom_provider(self):
        self.adapter.store.update('t', alias='effortlane-shadow')
        raw = request('turn/start', {'threadId': 't', 'model': 'effortlane-shadow', 'input': [{'type': 'text', 'text': 'hello'}]})
        self.adapter.client(raw)
        count = len(self.router.calls)
        steer = request('turn/steer', {'threadId': 't', 'input': []}, 3)
        self.assertEqual(self.adapter.client(steer), steer)
        self.assertEqual(len(self.router.calls), count)
        completed = {'jsonrpc': '2.0', 'method': 'turn/completed', 'params': {'threadId': 't', 'turn': {'status': 'failed'}}}
        self.adapter.server((json.dumps(completed)+'\n').encode())
        self.adapter.client(raw)
        self.assertIn('Previous turn failure', json.dumps(self.router.calls[-1][0]))
        self.router.mode = 'off'
        count = len(self.router.calls)
        self.adapter.client(raw)
        self.assertEqual(len(self.router.calls), count)
        custom = request('thread/start', {'model': 'effortlane-auto', 'modelProvider': 'ollama'})
        self.assertEqual(self.adapter.client(custom), custom)
        configured = request('thread/start', {'model': 'effortlane-auto', 'config': {'model': 'gpt-6-astra'}})
        self.assertEqual(json.loads(self.adapter.client(configured))['params']['model'], 'gpt-6-astra')

    def test_malformed_model_and_start_error_do_not_poison_active(self):
        raw = request('turn/start', {'threadId': 't', 'model': ['effortlane-auto'], 'input': []})
        self.assertEqual(self.adapter.client(raw), raw)
        self.adapter.store.update('t', alias='effortlane-auto')
        self.adapter.client(request('turn/start', {'threadId': 't', 'model': 'effortlane-auto', 'input': []}, 2))
        self.assertIn('t', self.adapter.active)
        error = b'{"jsonrpc":"2.0","id":2,"error":{"code":-1,"message":"opaque"}}\n'
        self.assertEqual(self.adapter.server(error), error)
        self.assertNotIn('t', self.adapter.active)

    def test_real_router_failure_floor(self):
        from core import Router
        catalog = {'models': [{'slug': slug, 'visibility': 'list', 'supported_reasoning_levels': [{'effort': 'medium'}]}
                              for slug in ('gpt-6-luna', 'gpt-6-sol')]}
        (self.root / 'native-models.json').write_text(json.dumps(catalog))
        (self.root / 'config.json').write_text(json.dumps({'mode': 'auto'}))
        real = Router(self.root / 'config.json', self.root / 'native-models.json',
                      self.root / 'state/leases.json', self.root / 'state/telemetry.jsonl',
                      jev_client=lambda *args: {'answers': {'capability': {'choice': 'luna'}, 'effort': {'choice': 'medium'}}})
        adapter = rpc_adapter.Adapter(self.root, router=real)
        adapter.store.update('t', alias='effortlane-auto', failed=True)
        sent = json.loads(adapter.client(request('turn/start', {'threadId': 't', 'model': 'effortlane-auto',
                                                                'input': [{'type': 'text', 'text': 'Continue please'}]})))
        self.assertEqual(sent['params']['model'], 'gpt-6-sol')

    def test_stdio_pump_with_fake_native_and_non_app_server_exec(self):
        native = self.root / 'fake-native'
        native.write_text('''#!%s
import json, sys
if sys.argv[1:2] != ['app-server'] and not (sys.argv[1:2] == ['-c'] and 'app-server' in sys.argv[1:]):
    print(json.dumps(sys.argv[1:]))
else:
    line = sys.stdin.readline()
    request = json.loads(line)
    print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{'thread':{'id':'native-thread'},'model':request['params']['model'],'seenModel':request['params']['model']}}), flush=True)
    print(json.dumps({'jsonrpc':'2.0','id':99,'method':'item/approval','params':{'opaque':'pass'}}), flush=True)
    print(json.dumps({'jsonrpc':'2.0','method':'thread/settings/updated','params':{'threadId':'native-thread','threadSettings':{'model':request['params']['model'],'effort':'medium'}}}), flush=True)
''' % sys.executable)
        native.chmod(0o700)
        adapter_path = Path(rpc_adapter.__file__)
        args = [sys.executable, str(adapter_path), '--native', str(native), '--root', str(self.root), '--']
        result = subprocess.run(args + ['app-server', '--analytics-default-enabled'],
                                input=request('thread/start', {'model': 'effortlane-auto'}), capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        lines = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(lines[0]['result']['seenModel'], 'gpt-6-sol')
        self.assertEqual(lines[0]['result']['model'], 'effortlane-auto')
        self.assertEqual(lines[1]['method'], 'item/approval')
        self.assertEqual(lines[2]['params']['threadSettings']['model'], 'effortlane-auto')
        desktop = subprocess.run(args + ['-c', 'features.code_mode_host=true', 'app-server', '--analytics-default-enabled',
                                         '-c', 'plugins.codex-app-tools@openai-bundled.mcp_servers.codex_app.enabled=true'],
                                 input=request('thread/start', {'model': 'effortlane-auto'}), capture_output=True, timeout=5)
        self.assertEqual(desktop.returncode, 0, desktop.stderr.decode())
        self.assertEqual(json.loads(desktop.stdout.splitlines()[0])['result']['seenModel'], 'gpt-6-sol')
        bypass = subprocess.run(args + ['exec', 'app-server'], capture_output=True, timeout=5)
        self.assertEqual(json.loads(bypass.stdout), ['exec', 'app-server'])

    def test_relay_survives_transform_failure_and_keeps_next_frame(self):
        source = io.BytesIO(b'first\nsecond\n')
        target = io.BytesIO()
        def transform(raw):
            if raw == b'first\n':
                raise TypeError('sanitized')
            return raw.upper()
        rpc_adapter._relay(source, target, transform)
        self.assertEqual(target.getvalue(), b'first\nSECOND\n')


if __name__ == '__main__':
    unittest.main()
