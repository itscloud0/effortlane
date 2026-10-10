import json
import unittest

import test_core as fixtures

payload = fixtures.payload


class CacheEstimateTests(unittest.TestCase):
    def estimate(self, **changes):
        from core import cache_switch_estimate
        sample = dict(cache_sample_model='gpt-6.1-sol', cache_sample_input=100000,
                      cache_sample_cached=96000, cache_sample_output=100,
                      cache_sample_age_s=10)
        sample.update(changes)
        return cache_switch_estimate('gpt-6.1-sol', 'gpt-6-luna', sample,
                                     {'cache_payback_requests': 1})

    def test_fresh_sample_prices_cache_rewrite_and_output(self):
        result = self.estimate()
        self.assertAlmostEqual(result['stay_usd'], .0186)
        self.assertAlmostEqual(result['switch_usd'], .01255)
        self.assertTrue(result['worth_switching'])

    def test_large_warm_context_tiny_reply_does_not_pay_in_one_request(self):
        result = self.estimate(cache_sample_input=200000, cache_sample_cached=200000,
                               cache_sample_output=1)
        self.assertFalse(result['worth_switching'])

    def test_invalid_or_stale_samples_are_unknown(self):
        for change in [dict(cache_sample_age_s=301), dict(cache_sample_age_s=-1),
                       dict(cache_sample_age_s=True), dict(cache_sample_cached=100001),
                       dict(cache_sample_input=True), dict(cache_sample_output=None),
                       dict(cache_sample_model='gpt-6-astra')]:
            with self.subTest(change=change):
                self.assertIsNone(self.estimate(**change))

    def test_unknown_rate_is_not_guessed(self):
        from core import cache_switch_estimate
        self.assertIsNone(cache_switch_estimate('gpt-7-sol', 'gpt-6-luna', {}, {}))

    def test_horizon_is_explicit_and_bounded(self):
        from core import cache_switch_estimate
        p = dict(cache_sample_model='gpt-6.1-sol', cache_sample_input=200000,
                 cache_sample_cached=200000, cache_sample_output=1, cache_sample_age_s=10)
        result = cache_switch_estimate('gpt-6.1-sol', 'gpt-6-luna', p, {})
        self.assertEqual(result['requests'], 2)
        self.assertTrue(result['worth_switching'])
        for horizon in [True, 0, 9, '2']:
            self.assertIsNone(cache_switch_estimate('gpt-6.1-sol', 'gpt-6-luna', p,
                                                  {'cache_payback_requests': horizon}))

    def test_long_context_rates_are_applied_per_request(self):
        result = self.estimate(cache_sample_input=300000, cache_sample_cached=300000,
                               cache_sample_output=100)
        self.assertAlmostEqual(result['stay_usd'], .0615)
        self.assertAlmostEqual(result['switch_usd'], .075075)


class MeasuredGuardTests(unittest.TestCase):
    new_router = fixtures.RouterTest.new_router
    def setUp(self):
        fixtures.RouterTest.setUp(self)
        c = json.loads((self.root / 'catalog.json').read_text())
        sol = next(x for x in c['models'] if x['slug'] == 'gpt-6-sol')
        sol['slug'] = 'gpt-6.1-sol'
        (self.root / 'catalog.json').write_text(json.dumps(c))
        cfg = json.loads((self.root / 'config.json').read_text())
        cfg.update(auto_policy='completion_v4', cache_payback_requests=1)
        (self.root / 'config.json').write_text(json.dumps(cfg))

    def test_measured_downgrade_does_not_wait_three_turns(self):
        router = self.new_router(lambda *args: {'answers': {
            'work_shape': {'choice': 'mechanical'}, 'effort': {'choice': 'low'}}})
        p = payload('Rename the local test variable', context_tokens=100000)
        p.update(current_model='gpt-6.1-sol', cached_input_pct=96,
                 cache_sample_model='gpt-6.1-sol', cache_sample_input=100000,
                 cache_sample_cached=96000, cache_sample_output=100, cache_sample_age_s=10)
        result = router.decide(p, session_id='measured', native_selection=True)
        self.assertEqual(result['model'], 'gpt-6-luna')
        self.assertEqual(result['cache_guard_basis'], 'projection')
        router.record_usage(result, None, 'ok', event='route')
        row = json.loads((self.root / 'telemetry.jsonl').read_text())
        self.assertEqual(row['cache_guard_basis'], 'projection')
        self.assertGreater(row['cache_switch_estimate']['stay_usd'], row['cache_switch_estimate']['switch_usd'])

    def test_unprofitable_downgrade_does_not_unlock_after_three_proposals(self):
        router = self.new_router(lambda *args: {'answers': {
            'work_shape': {'choice': 'mechanical'}, 'effort': {'choice': 'low'}}})
        for i in range(4):
            p = payload(f'Rename local variable number {i}', context_tokens=200000)
            p.update(current_model='gpt-6.1-sol', cached_input_pct=100,
                     cache_sample_model='gpt-6.1-sol', cache_sample_input=200000,
                     cache_sample_cached=200000, cache_sample_output=1, cache_sample_age_s=10)
            result = router.decide(p, session_id='hold', native_selection=True)
            self.assertEqual(result['model'], 'gpt-6.1-sol')
            self.assertEqual(result['reason'], 'cache_hysteresis')


    def test_shadow_keeps_executor_and_selected_effort(self):
        cfg = json.loads((self.root / 'config.json').read_text())
        cfg['shadow_policy'] = 'completion_v4'
        (self.root / 'config.json').write_text(json.dumps(cfg))
        router = self.new_router(lambda *args: {'answers': {
            'work_shape': {'choice': 'mechanical'}, 'effort': {'choice': 'low'}}})
        p = payload('Rename a test variable', model='effortlane-shadow', context_tokens=100000)
        p.update(current_model='gpt-6.1-sol', shadow_executor_effort='high',
                 cache_sample_model='gpt-6.1-sol', cache_sample_input=100000,
                 cache_sample_cached=96000, cache_sample_output=100, cache_sample_age_s=10)
        r = router.decide(p, session_id='shadow', native_selection=True)
        self.assertEqual((r['model'], r['effort']), ('gpt-6.1-sol', 'high'))
        self.assertEqual(r['proposed_model'], 'gpt-6-luna')
        self.assertEqual(r['cache_guard_basis'], 'projection')

    def test_upgrade_and_manual_choice_bypass_downgrade_estimate(self):
        router = self.new_router(lambda *args: {'answers': {
            'work_shape': {'choice': 'substantive'}, 'effort': {'choice': 'high'}}})
        p = payload('Debug a difficult race condition', context_tokens=300000)
        p.update(current_model='gpt-6-luna', cache_sample_model='gpt-6-luna',
                 cache_sample_input=300000, cache_sample_cached=300000,
                 cache_sample_output=1, cache_sample_age_s=10)
        r = router.decide(p, session_id='upgrade', native_selection=True)
        self.assertEqual(r['model'], 'gpt-6.1-sol')
        self.assertNotIn('cache_switch_estimate', r)
        p['model'] = 'gpt-6-luna'
        r = router.decide(p, session_id='manual', native_selection=True)
        self.assertEqual(r['model'], 'gpt-6-luna')
        self.assertEqual(r['reason'], 'concrete_model')
