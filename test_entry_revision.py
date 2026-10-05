"""Regression tests for partial coverage, independent portfolios and entry safety."""
import copy
import gzip
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from auto_runner import run, exact_quote
from paper_core import BASE_STRATEGIES, CAPITAL, STRATEGIES, fresh_state, migrate_state, run_cycle
from pipeline import select_atm_expiry, atm_snapshot, ScanConfig
from scan_recovery import coverage_gate
from scan_schedule import schedule_diagnostics
from test_pipeline import NOW, EPOCH, CFG, candidate, entry


def partial_gate():
    return coverage_gate(dict(stage2_planned=list(range(180)), stage3_planned=list(range(120)),
        stage2=[{'status':'checked'}]*104, stage3=[{'status':'complete'}]*120), CFG)


class ExpiryTests(unittest.TestCase):
    def test_preferred_then_fallback_and_boundaries(self):
        for days, expected in [([21, 29, 31, 44, 46, 60], 31), ([21, 29, 46, 60], 29),
                               ([14, 49, 70], 49), ([21], 21), ([60], 60), ([30,45], 30)]:
            with self.subTest(days=days):
                expiries = [(NOW.date()+timedelta(days=d)).isoformat() for d in days]
                expiry, dte, reason = select_atm_expiry(expiries, NOW.date())
                self.assertEqual(dte, expected)
                self.assertEqual(expiry, (NOW.date()+timedelta(days=expected)).isoformat())
                self.assertEqual('fallback' in reason, not any(30 <= d <= 45 for d in days))
        for days in ([], [20,61]):
            with self.assertRaisesRegex(ValueError, '21–60'):
                select_atm_expiry([(NOW.date()+timedelta(days=d)).isoformat() for d in days], NOW.date())

    def test_failure_keeps_selected_expiry_without_retrying_another(self):
        expiries = [(NOW.date()+timedelta(days=d)).isoformat() for d in (32, 49)]
        with patch('pipeline.ticker_for'), patch('pipeline.get_option_expirations', return_value=(expiries, '')), \
             patch('pipeline.get_option_chain', return_value=(None, 'Yahoo 401')) as chain:
            result = atm_snapshot({'Ticker':'TEST','Stock Price':110.,'HV30':.3}, NOW.date())
        self.assertEqual(chain.call_count, 1)
        self.assertEqual(result['expiry'], expiries[0])
        self.assertEqual(result['expiry_reason'], 'preferred 30–45 DTE')
        self.assertEqual(result['error'], 'Yahoo 401')
        self.assertEqual(result['status'], 'unavailable')


class PositionExperimentTests(unittest.TestCase):
    def test_fifteen_position_migration_preserves_existing_five_eight_ten_models(self):
        old = run_cycle(fresh_state(NOW.isoformat()), [candidate()], {}, {}, NOW.isoformat(), 'one')
        old['models'] = {k:v for k,v in old['models'].items() if not k.endswith('_P15')}
        before = copy.deepcopy(old)
        start = (NOW+timedelta(days=1)).isoformat()
        new = migrate_state(old, start)
        self.assertEqual(old, before)
        for k, model in old['models'].items():
            self.assertEqual(new['models'][k], model)
        self.assertEqual(set(new['migrations'][-1]['added_models']), {k+'_P15' for k in BASE_STRATEGIES})
        for k in BASE_STRATEGIES:
            self.assertEqual(new['models'][k+'_P15']['created'], start)
            self.assertEqual(new['models'][k+'_P15']['cash'], CAPITAL)
            self.assertEqual(new['models'][k+'_P15']['positions'], [])
        self.assertEqual(new, migrate_state(new, start))

    def test_all_variants_differ_only_in_name_and_limit(self):
        self.assertEqual(len(BASE_STRATEGIES), 13)
        self.assertEqual(len(STRATEGIES), 52)
        for key in BASE_STRATEGIES:
            self.assertEqual(STRATEGIES[key]['max_positions'], 5)
            for limit in (8,10,15):
                actual = dict(STRATEGIES[f'{key}_P{limit}'])
                actual.update(name=STRATEGIES[key]['name'], max_positions=5)
                self.assertEqual(actual, STRATEGIES[key])

    def test_five_eight_ten_fifteen_caps_and_diagnostics(self):
        rows = [candidate(f'T{i:02}', strike=50., pre_gate_qualified=True, verified_at=EPOCH) for i in range(20)]
        result = run_cycle(fresh_state(NOW.isoformat()), rows, {}, {}, NOW.isoformat(), 'one')
        for key, model in result['models'].items():
            limit = STRATEGIES[key]['max_positions']
            self.assertEqual(len(model['positions']), limit)
            metric = result['strategy_diagnostics'][key]
            self.assertEqual(metric['qualifying_contracts'], 20)
            self.assertEqual(metric['verified_contracts'], 20)
            self.assertEqual(metric['executed_opens'], limit)
            self.assertEqual(metric['remaining_slots'], 0)
            self.assertEqual(metric['rejection_counts'][f'concentration: {limit}-position limit'], 20-limit)

    def test_collateral_and_reserve_apply_to_every_limit(self):
        rows = [candidate(f'T{i:02}', strike=200.) for i in range(20)]
        rows += [candidate('TOOBIG', strike=200.01)]
        result = run_cycle(fresh_state(NOW.isoformat()), rows, {}, {}, NOW.isoformat(), 'one')
        for key, model in result['models'].items():
            self.assertEqual(len(model['positions']), 4)
            self.assertGreaterEqual(model['cash']-sum(p['strike']*100 for p in model['positions']), CAPITAL*.1)
            self.assertIn('collateral: above position limit', result['strategy_diagnostics'][key]['rejection_counts'])
            self.assertIn('collateral: cash reserve', result['strategy_diagnostics'][key]['rejection_counts'])

    def test_cash_reserve_includes_fee_without_future_premium(self):
        for balance, opens in [(30000, 0), (30001, 1)]:
            state = fresh_state(NOW.isoformat())
            for model in state['models'].values():
                model['cash'] = balance
            result = run_cycle(state, [candidate(strike=200.)], {}, {}, NOW.isoformat(), 'one')
            self.assertTrue(all(len(m['positions']) == opens for m in result['models'].values()))

    def test_additive_v2_migration_preserves_every_existing_field(self):
        old = run_cycle(fresh_state(NOW.isoformat()), [candidate()], {}, {}, NOW.isoformat(), 'one')
        old['models'] = {k:v for k,v in old['models'].items() if k in BASE_STRATEGIES}
        old['version'] = 2
        old['last_audit'] = {'immutable':'original audit reference'}
        before = copy.deepcopy(old)
        start = (NOW+timedelta(days=1)).isoformat()
        new = migrate_state(old, start)
        self.assertEqual(old, before)
        for k in BASE_STRATEGIES:
            self.assertEqual(new['models'][k], old['models'][k])
        self.assertEqual(new['last_audit'], old['last_audit'])
        self.assertEqual(len(new['migrations'][-1]['added_models']), 39)
        for k in set(STRATEGIES)-set(BASE_STRATEGIES):
            self.assertEqual(new['models'][k]['created'], start)
            self.assertEqual(new['models'][k]['cash'], CAPITAL)
            self.assertEqual(new['models'][k]['history'], [])
        self.assertEqual(new, migrate_state(new, (NOW+timedelta(days=2)).isoformat()))
        old['models'].pop('A40')
        with self.assertRaisesRegex(ValueError, 'refusing to reset'):
            migrate_state(old, start)


class EntrySafetyTests(unittest.TestCase):
    def evaluate(self, rows=None, quote=None, gate=None, now=NOW, cache_status='complete', side_effect=None, time_source=None):
        rows = rows if rows is not None else [candidate()]
        quote = quote if quote is not None else dict(bid=2.,ask=2.2,observed_at=EPOCH,open_interest=500)
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        def scan(state, *args, **kwargs):
            state['option_cache'] = {c['ticker']:entry([c], cache_status) for c in rows}
            return dict(stage1=[], stage2=[], stage3=[], eligible_symbols=[c['ticker'] for c in rows],
                warnings=[], entry_gate=gate if gate is not None else partial_gate())
        sched = pd.Series({'market_open':NOW-timedelta(hours=1), 'market_close':NOW+timedelta(hours=5)})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'
            with patch('auto_runner.datetime', Clock), patch('auto_runner.session', return_value=sched), \
                 patch('auto_runner.load_universe', return_value={'symbols':[c['ticker'] for c in rows]}), \
                 patch('auto_runner.scan_pipeline', side_effect=scan), patch('auto_runner.time.time', side_effect=time_source or (lambda: now.timestamp())), \
                 patch('auto_runner.exact_quote', return_value=quote, side_effect=side_effect), redirect_stdout(io.StringIO()):
                run(path, CFG)
            saved = json.loads(path.read_text())
            audit = json.loads(gzip.decompress((path.parent/saved['last_run']['audit_file']).read_bytes()))
        return saved, audit

    def assert_blocked(self, saved, audit, reason):
        self.assertTrue(all(not m['positions'] for m in saved['models'].values()))
        self.assertTrue(audit['portfolio_constraints'])
        self.assertTrue(all(reason in r['reasons'] for r in audit['portfolio_constraints']))

    def test_partial_coverage_opens_verified_contracts_with_warning(self):
        saved, audit = self.evaluate()
        self.assertEqual(saved['last_run']['qualifying_contracts'], 1)
        self.assertEqual(saved['last_run']['verified_contracts'], 1)
        self.assertEqual(saved['last_run']['selected_entries'], len(STRATEGIES))
        self.assertEqual(saved['last_run']['coverage_blocked_contracts'], 0)
        self.assertFalse(saved['last_run']['entry_gate']['coverage_ok'])
        self.assertTrue(any('QUALITY WARNING' in w for w in saved['last_run']['warnings']))
        self.assertEqual(saved['strategy_diagnostics']['A']['executed_opens'], 1)

    def test_contract_safety_remains_strict_under_partial_coverage(self):
        for changes, reason in [({'earnings_known':False}, 'earnings unknown'),
                                ({'earnings':'YES'}, 'earnings in period'),
                                ({'fetched':EPOCH-1801}, 'stale data: cache freshness'),
                                ({'fetched':EPOCH+1}, 'stale data: cache freshness'),
                                ({'open_interest':0}, 'open interest'),
                                ({'bid':0}, 'spread or invalid bid/ask'),
                                ({'ask':4.}, 'spread or invalid bid/ask'),
                                ({'expiry':'2026-09-30'}, 'DTE outside current configured range'),
                                ({'expiry':'2026-12-31'}, 'DTE outside current configured range'),
                                ({'strike':201.}, 'collateral outside current configured range')]:
            with self.subTest(changes=changes):
                self.assert_blocked(*self.evaluate([candidate(**changes)]), reason)
        self.assert_blocked(*self.evaluate(cache_status='partial'), 'stale data: latest scan unavailable or incomplete')

    def test_exact_quote_safety_and_yahoo_failures(self):
        for changes, reason in [({'observed_at':EPOCH-46}, 'stale data: exact quote verification'),
                                ({'observed_at':None}, 'stale data: exact quote verification'),
                                ({'observed_at':EPOCH+1}, 'stale data: exact quote verification'),
                                ({'open_interest':99}, 'open interest on entry verification'),
                                ({'open_interest':None}, 'open interest on entry verification'),
                                ({'bid':float('nan')}, 'invalid exact bid/ask'),
                                ({'ask':float('inf')}, 'invalid exact bid/ask'),
                                ({'bid':1.9}, 'quote changed adversely: rescan required')]:
            with self.subTest(changes=changes):
                q = dict(bid=2., ask=2.2, observed_at=EPOCH, open_interest=500, **{})
                q.update(changes)
                self.assert_blocked(*self.evaluate(quote=q), reason)
        self.assert_blocked(*self.evaluate(side_effect=ValueError('Yahoo 401')), 'exact quote unavailable')
        gate = dict(partial_gate(), allowed=False, reason='Yahoo access cooldown: new entries paused')
        self.assert_blocked(*self.evaluate(gate=gate), gate['reason'])

    def test_market_close_and_cutoff_still_block(self):
        for now in (NOW+timedelta(hours=4,minutes=45), NOW+timedelta(hours=5)):
            with self.subTest(now=now):
                rows = [candidate(fetched=now.timestamp())]
                self.assert_blocked(*self.evaluate(rows, now=now), 'entry window closed')

    def test_exact_contract_and_same_day_trade_required(self):
        for contract, traded in [('OTHER',NOW), (candidate()['contract'],NOW-timedelta(days=1))]:
            frame = pd.DataFrame([dict(contractSymbol=contract, bid=2., ask=2.2, lastTradeDate=traded, openInterest=500)])
            with patch('auto_runner.yf.Ticker') as ticker:
                ticker.return_value.option_chain.return_value.puts = frame
                with self.assertRaises(ValueError):
                    exact_quote(candidate(), NOW.date())

    def test_early_verification_cannot_go_stale_while_later_contracts_verify(self):
        clock = [EPOCH]
        def quote(c, *args):
            if c['ticker'] == 'B':
                clock[0] += 46
            return dict(bid=2., ask=2.2, observed_at=clock[0], open_interest=500)
        saved, audit = self.evaluate([candidate('A'), candidate('B')], side_effect=quote, time_source=lambda: clock[0])
        self.assertTrue(all([p['ticker'] for p in m['positions']] == ['B'] for m in saved['models'].values()))
        self.assertTrue(all('stale data: exact quote verification' in r['reasons']
                            for r in audit['portfolio_constraints'] if r['ticker'] == 'A'))

    def test_before_market_open_does_not_trade(self):
        saved, _ = self.evaluate(now=NOW-timedelta(hours=2))
        self.assertTrue(all(not m['positions'] for m in saved['models'].values()))
        self.assertEqual(saved['last_run']['status'], 'Market closed')


class CadenceTests(unittest.TestCase):
    def test_missing_windows_queue_delay_and_unscheduled_runs(self):
        rows = [dict(id=1, event='schedule', created_at='2026-10-02T13:07:00Z', run_started_at='2026-10-02T13:09:00Z'),
                dict(id=2, event='workflow_dispatch', created_at='2026-10-02T13:40:00Z')]
        result = schedule_diagnostics(rows, datetime(2026,10,2,14,20,tzinfo=timezone.utc))
        self.assertEqual(result['expected_windows'], 2)
        self.assertEqual(result['unobserved_windows'], 1)
        self.assertEqual(result['runs'][0]['queue_delay_seconds'], 120)
        self.assertFalse(schedule_diagnostics([], NOW)['available'])

    def test_weekend_and_reporting_grace(self):
        rows = [dict(id=1,event='schedule',created_at='2026-10-02T21:37:00Z')]
        result = schedule_diagnostics(rows, datetime(2026,10,4,14,0,tzinfo=timezone.utc))
        self.assertEqual(result['expected_windows'], 1)
        self.assertEqual(result['unobserved_windows'], 0)
        result = schedule_diagnostics(rows, datetime(2026,10,2,22,10,tzinfo=timezone.utc))
        self.assertEqual(result['expected_windows'], 0)


if __name__ == '__main__':
    unittest.main()
