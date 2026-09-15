"""Synthetic fixtures only; no test contacts Yahoo or production paper state."""
import copy
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock
from types import SimpleNamespace

import pandas as pd

from engine import apply_put_scores, scan_put_ticker, scan_calls_for_basis, normalize_dates
from paper_core import fresh_state, migrate_state, run_cycle, STRATEGIES, VERSION
from pipeline import (ScanConfig, candidate_from_row, rolling_ranking, best_by_weight,
                      missed_opportunities, scan_pipeline, atm_snapshot, rotate, clean)
from scoring import opportunity_score
from universe import prescreen_underlyings, _extract_symbol_frame
from auto_runner import run, NY, exact_quote

NOW = datetime(2026, 9, 14, 11, 0, tzinfo=NY)
EPOCH = NOW.timestamp()
CFG = ScanConfig()


def option_row(ticker='TEST', **changes):
    r = {'Ticker':ticker, 'Contract':ticker+'261016P00100000', 'Expiry':'2026-10-16',
         'Strike':100., 'DTE':32, 'Bid':2., 'Ask':2.2, 'Contract IV':.4,
         'Stock Price':110., 'Observed At':EPOCH, 'Return / Day':.0015,
         'Protection Ratio':1.2, 'Expected Move':.1, 'Cushion':.12,
         'ATM IV':.4, 'HV30':.3, 'Open Interest':500,
         'Earnings in Period':'No', 'Earnings Known':True, 'Last Trade Date':NOW}
    return dict(r, **changes)


def candidate(ticker='TEST', **changes):
    return dict(candidate_from_row(option_row(ticker), NOW.date(), CFG), **changes)


def entry(contracts, status='complete'):
    return dict(contracts=contracts, status=status, scanned_at=EPOCH,
                best_by_weight=best_by_weight(contracts))


class ScoringAndStateTests(unittest.TestCase):
    def test_weights_match_existing_formula_without_rounding_components(self):
        for w in (30, 40, 50, 60):
            df = pd.DataFrame({'Return / Day':[.001234567], 'Protection Ratio':[.8734567]})
            expected = apply_put_scores(df, w)['Opportunity Index'].iloc[0]
            self.assertEqual(opportunity_score(123.4567, 87.34567, w), expected)
        self.assertEqual(opportunity_score(0, 100, 30), 0)
        with self.assertRaises(ValueError):
            opportunity_score(100, 100, 101)

    def test_variants_choose_different_contracts_same_ticker(self):
        a = candidate(contract='return', return_score=200., protection_score=80.)
        b = candidate(contract='protection', return_score=80., protection_score=200.)
        result = run_cycle(fresh_state(NOW.isoformat()), [a, b], {}, {}, NOW.isoformat(), 'one')
        self.assertEqual(result['models']['A']['positions'][0]['contract'], 'protection')
        self.assertEqual(result['models']['A60']['positions'][0]['contract'], 'return')
        self.assertEqual(best_by_weight([a,b])['60']['contract'], 'return')
        for weight in (40, 50, 60):
            cfg = dict(STRATEGIES[f'A{weight}'])
            cfg.pop('name'); cfg.pop('return_weight')
            base = dict(STRATEGIES['A']); base.pop('name'); base.pop('return_weight')
            self.assertEqual(cfg, base)
        self.assertGreater(len(STRATEGIES), 10)

    def test_additive_migration_preserves_portfolios_and_idempotency(self):
        old = run_cycle(fresh_state(NOW.isoformat()), [candidate()], {}, {}, NOW.isoformat(), 'one')
        old['version'] = 1
        for k in ('A40', 'A50', 'A60'):
            old['models'].pop(k)
        original = copy.deepcopy(old)
        result = migrate_state(old, '2026-09-14T11:30:00-04:00')
        self.assertEqual(old, original)
        self.assertEqual(result['version'], VERSION)
        for key in original['models']:
            self.assertEqual(result['models'][key], original['models'][key])
        self.assertEqual(result['models']['A40']['cash'], 100000)
        self.assertFalse(result['models']['A40']['positions'])
        self.assertEqual(result, migrate_state(result, '2026-09-14T12:00:00-04:00'))
        for broken in (dict(old, version=99), dict(old, models={})):
            with self.assertRaises(ValueError):
                migrate_state(broken, NOW.isoformat())

    def test_portfolio_audit_keeps_all_rejection_reasons(self):
        bad = candidate('BAD', strike=250, rejections=['earnings in period', 'spread'])
        result = run_cycle(fresh_state(NOW.isoformat()), [bad, candidate()], {}, {}, NOW.isoformat(), 'one')
        rows = [r for r in result['entry_audit'] if r['strategy']=='A']
        rejected = next(r for r in rows if r['ticker']=='BAD')
        self.assertIn('earnings in period', rejected['reasons'])
        self.assertIn('collateral: above position limit', rejected['reasons'])
        self.assertEqual(next(r for r in rows if r['ticker']=='TEST')['decision'], 'selected')
        many = [candidate(str(i)) for i in range(8)]
        result = run_cycle(fresh_state(NOW.isoformat()), many, {}, {}, NOW.isoformat(), 'two')
        self.assertEqual(len([r for r in result['entry_audit'] if r['strategy']=='A']), 8)
        self.assertEqual(sum(r['decision']=='selected' for r in result['entry_audit'] if r['strategy']=='A'), 5)


class CacheTests(unittest.TestCase):
    def test_freshness_boundaries_and_failed_refresh(self):
        cache = {'TEST':entry([candidate()])}
        self.assertFalse(rolling_ranking(cache, EPOCH+1800, CFG, {'TEST'})[0]['rejections'])
        self.assertIn('stale data: cache freshness', rolling_ranking(cache, EPOCH+1801, CFG, {'TEST'})[0]['rejections'])
        self.assertFalse(rolling_ranking(cache, EPOCH+1801, ScanConfig(freshness_minutes=40), {'TEST'})[0]['rejections'])
        self.assertTrue(rolling_ranking(cache, EPOCH-1, CFG, {'TEST'})[0]['rejections'])
        self.assertTrue(rolling_ranking(cache, EPOCH, CFG, set())[0]['rejections'])
        self.assertIn('collateral outside current configured range',
            rolling_ranking(cache, EPOCH, ScanConfig(max_cash=9000), {'TEST'})[0]['rejections'])
        cache['TEST']['status'] = 'unavailable'
        self.assertTrue(rolling_ranking(cache, EPOCH, CFG, {'TEST'})[0]['rejections'])
        self.assertEqual(cache['TEST']['contracts'][0]['score'], candidate()['score'])

    def test_previous_batch_can_win(self):
        old = candidate('OLD', score=180, return_score=180, protection_score=180, fetched=EPOCH-300)
        new = candidate('NEW')
        rows = rolling_ranking({'OLD':entry([old]), 'NEW':entry([new])}, EPOCH, CFG, {'OLD','NEW'})
        result = run_cycle(fresh_state(NOW.isoformat()), rows, {}, {}, NOW.isoformat(), 'one')
        self.assertEqual(result['models']['A']['positions'][0]['ticker'], 'OLD')

    def test_earnings_and_liquidity_rejections_retain_scores(self):
        r = option_row(**{'Earnings in Period':'YES · 2026-09-20', 'Open Interest':5, 'Ask':4., 'Last Trade Date':NOW-timedelta(days=1)})
        c = candidate_from_row(r, NOW.date(), CFG)
        self.assertGreater(c['score'], 0)
        self.assertEqual(set(c['rejections']), {'earnings in period', 'open interest', 'spread or invalid bid/ask', 'stale trade data'})
        self.assertFalse(best_by_weight([c]))
        self.assertEqual(len(rolling_ranking({'TEST':entry([c])}, EPOCH, CFG, {'TEST'})), 1)

    def test_miss_audit_compares_unique_tickers_and_weights(self):
        primary = [candidate('P', return_score=100, protection_score=100)]
        rotated = candidate('R', return_score=150, protection_score=150)
        discoveries = missed_opportunities(primary+[rotated], ['P'], ['R'])
        self.assertEqual(len(discoveries), 4)
        self.assertTrue(all(r['improvement']==50 for r in discoveries))
        self.assertFalse(missed_opportunities(primary+[dict(rotated, rejections=['earnings'])], ['P'], ['R']))


class PipelineTests(unittest.TestCase):
    def test_missing_earnings_dates_do_not_break_comparisons(self):
        dates = normalize_dates([None, pd.NaT, float('nan'), 'not a date', '2026-10-16'])
        self.assertEqual(dates, [datetime(2026, 10, 16).date()])
        self.assertTrue(all(d >= NOW.date() for d in dates))

    def test_full_scan_scores_all_qualifying_expiries_and_audits_exclusions(self):
        contracts = pd.DataFrame([dict(strike=k, bid=2., ask=2.2, impliedVolatility=.4,
            contractSymbol=f'TEST-{k}', openInterest=500, lastTradeDate=NOW) for k in (20,100,105,120)])
        chain = SimpleNamespace(puts=contracts, calls=contracts)
        audit = []
        with patch('engine.yf.Ticker'), patch('engine.get_stock_price', return_value=110.), \
             patch('engine.get_historical_volatility', return_value=.3), \
             patch('engine.get_earnings_dates', return_value=[(NOW+timedelta(days=5)).date()]), \
             patch('engine.get_option_expirations', return_value=(['2026-10-16','2026-10-23'], '')), \
             patch('engine.get_option_chain', return_value=(chain, '')):
            frame, warnings = scan_put_ticker('TEST', 21, 60, 3000, 20000, 0, audit_rows=audit, asof=NOW.date())
        self.assertEqual(len(frame), 4)
        self.assertEqual(set(frame['Strike']), {100,105})
        self.assertTrue(frame['Has Earnings'].all())
        self.assertTrue((frame['Opportunity Index'] > 0).all())
        self.assertFalse(warnings)
        self.assertEqual(len(audit), 4)
        self.assertTrue(any('not OTM' in r['rejections'] for r in audit))
        self.assertTrue(any('collateral outside configured range' in r['rejections'] for r in audit))

    def test_covered_calls_still_allow_earnings(self):
        today = datetime.now().date()
        expiry = (today+timedelta(days=30)).isoformat()
        chain = SimpleNamespace(calls=pd.DataFrame([dict(strike=120., bid=2., ask=2.2, contractSymbol='CALL')]))
        with patch('engine.yf.Ticker'), patch('engine.get_stock_price', return_value=110.), \
             patch('engine.get_earnings_dates', return_value=[today+timedelta(days=5)]), \
             patch('engine.get_option_expirations', return_value=([expiry], '')), \
             patch('engine.get_option_chain', return_value=(chain, '')):
            frame = scan_calls_for_basis('TEST', 100)
        self.assertEqual(len(frame), 1)
        self.assertTrue(frame.iloc[0]['Earnings in Period'])
        self.assertIn('Allowed', frame.iloc[0]['Earnings Policy'])

    def test_entry_verification_reuses_expiry_but_matches_exact_contract(self):
        frame = pd.DataFrame([dict(contractSymbol='A', bid=2., ask=2.2, lastTradeDate=NOW),
                              dict(contractSymbol='B', bid=3., ask=3.2, lastTradeDate=NOW)])
        with patch('auto_runner.yf.Ticker') as ticker:
            ticker.return_value.option_chain.return_value = SimpleNamespace(puts=frame)
            cache = {}
            a = exact_quote(dict(ticker='TEST', expiry='2026-10-16', contract='A'), NOW.date(), cache)
            b = exact_quote(dict(ticker='TEST', expiry='2026-10-16', contract='B'), NOW.date(), cache)
            self.assertEqual(ticker.return_value.option_chain.call_count, 1)
            self.assertEqual(a['bid'], 2.)
            self.assertEqual(b['bid'], 3.)
            with self.assertRaises(ValueError):
                exact_quote(dict(ticker='TEST', expiry='2026-10-16', contract='MISSING'), NOW.date(), cache)

    def test_broad_stages_and_rotation(self):
        symbols = [f'S{i:03}' for i in range(240)]
        pre = pd.DataFrame([{'Ticker':s, 'Eligible':True, 'Price Date':NOW.isoformat(),
                             'Stock Price':110., 'HV30':.3} for s in symbols])
        state = fresh_state(NOW.isoformat())
        def atm(r, today):
            return dict(ticker=r['Ticker'], prescreen_score=2., status='checked', earnings_in_period=True)
        def scan(symbol, *args, **kwargs):
            return pd.DataFrame([option_row(symbol)]), []
        with patch('pipeline.prescreen_underlyings', return_value=(pre, [])) as underlying, \
             patch('pipeline.atm_snapshot', side_effect=atm), \
             patch('pipeline.scan_put_ticker', side_effect=scan), patch('pipeline.time.time', return_value=EPOCH):
            report = scan_pipeline(state, {'symbols':symbols}, NOW, CFG, time.monotonic())
            self.assertEqual(len(underlying.call_args.args[0]), 240)
            self.assertEqual(len(report['stage1']), 240)
            self.assertEqual(len(report['stage2']), 180)
            self.assertEqual(len(report['primary']), 100)
            self.assertEqual(len(report['rotating']), 20)
            self.assertEqual(len(report['stage3']), 120)
            self.assertTrue(all(r['status']=='complete' for r in report['stage3']))
            self.assertEqual(len(report['rolling_ranking']), 120)
            second = scan_pipeline(state, {'symbols':symbols}, NOW, CFG, time.monotonic())
            self.assertNotEqual(report['rotating'], second['rotating'])
            self.assertNotEqual(report['stage2_planned'], second['stage2_planned'])
            json.dumps(clean(state), allow_nan=False)

    def test_yahoo_failure_invalidates_previous_data_without_fabrication(self):
        pre = pd.DataFrame([{'Ticker':'TEST', 'Eligible':True, 'Price Date':NOW.isoformat(), 'Stock Price':110., 'HV30':.3}])
        state = dict(option_cache={'TEST':entry([candidate()])})
        with patch('pipeline.prescreen_underlyings', return_value=(pre, [])), \
             patch('pipeline.atm_snapshot', side_effect=RuntimeError('Yahoo 401')), \
             patch('pipeline.scan_put_ticker', side_effect=RuntimeError('Yahoo 429')), \
             patch('pipeline.time.time', return_value=EPOCH):
            report = scan_pipeline(state, {'symbols':['TEST']}, NOW, CFG, time.monotonic())
        self.assertEqual(report['stage3'][0]['status'], 'unavailable')
        self.assertEqual(state['option_cache']['TEST']['contracts'], [candidate()])
        self.assertTrue(report['rolling_ranking'][0]['rejections'])
        self.assertEqual(len(report['warnings']), 2)

    def test_atm_uses_richness_not_raw_iv_and_keeps_earnings(self):
        frame = pd.DataFrame([dict(strike=100, impliedVolatility=.8, bid=3., ask=3.3, openInterest=500, volume=20)])
        chain = SimpleNamespace(calls=frame, puts=frame)
        with patch('pipeline.yf.Ticker'), patch('pipeline.get_option_expirations', return_value=(['2026-10-16'], '')), \
             patch('pipeline.get_option_chain', return_value=(chain, '')), \
             patch('pipeline.get_earnings_dates', return_value=[(NOW+timedelta(days=5)).date()]):
            a = atm_snapshot({'Ticker':'A','Stock Price':100.,'HV30':.4}, NOW.date())
            b = atm_snapshot({'Ticker':'B','Stock Price':100.,'HV30':.8}, NOW.date())
        self.assertEqual(a['iv_richness'], 2)
        self.assertGreater(a['prescreen_score'], b['prescreen_score'])
        self.assertTrue(a['earnings_in_period'])
        self.assertEqual(a['status'], 'checked')

    def test_missing_underlying_is_visible_and_cannot_copy_another_ticker(self):
        raw = pd.DataFrame([[100]], columns=pd.MultiIndex.from_tuples([('A','Close')]))
        self.assertTrue(_extract_symbol_frame(raw, 'B').empty)
        prescreen_underlyings.clear()
        with patch('universe.yf.download', return_value=pd.DataFrame()), patch('universe.yf.Ticker') as ticker:
            ticker.return_value.history.side_effect = RuntimeError('unavailable')
            frame, warnings = prescreen_underlyings(('MISSING',), 3000, 20000, include_rejected=True)
        self.assertEqual(len(frame), 1)
        self.assertFalse(frame.iloc[0]['Eligible'])
        self.assertIn('unavailable', frame.iloc[0]['Rejection Reasons'])
        self.assertTrue(warnings)

    def test_config_and_rotation_validation(self):
        for changes in ({'stage2_limit':149}, {'stage3_limit':101}, {'freshness_minutes':0}):
            with self.assertRaises(ValueError):
                ScanConfig(**changes)
        self.assertEqual(rotate(['A','B','C'], 2, 2), (['C','A'], 1))


class RunnerTests(unittest.TestCase):
    def test_entry_quote_failure_is_audited_without_a_fill(self):
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW
        def scan(state, *args):
            state['option_cache'] = {'TEST':entry([candidate()])}
            return dict(stage1=[], stage2=[], stage3=[], eligible_symbols=['TEST'], warnings=[], entry_gate={'allowed':True, 'reason':''})
        sched = pd.Series({'market_open':NOW-timedelta(hours=1), 'market_close':NOW+timedelta(hours=5)})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'
            with patch('auto_runner.datetime', Clock), patch('auto_runner.session', return_value=sched), \
                 patch('auto_runner.load_universe', return_value={'symbols':['TEST']}), \
                 patch('auto_runner.scan_pipeline', side_effect=scan), patch('auto_runner.time.time', return_value=EPOCH), \
                 patch('auto_runner.exact_quote', side_effect=ValueError('Yahoo unavailable')):
                run(path, CFG)
            saved = json.loads(path.read_text())
            self.assertTrue(all(not m['positions'] for m in saved['models'].values()))
            import gzip
            audit = json.loads(gzip.decompress((path.parent/saved['last_run']['audit_file']).read_bytes()))
            self.assertTrue(all('exact quote unavailable' in r['reasons'] for r in audit['portfolio_constraints']))
            self.assertTrue(saved['last_run']['warnings'])

    def test_runner_writes_audit_and_preserves_legacy_state(self):
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW
        def scan(state, universe, now, config, started):
            state['option_cache'] = {'TEST':entry([candidate()])}
            return dict(stage1=[{'Ticker':'TEST','Eligible':True}], stage2=[], stage3=[],
                        eligible_symbols=['TEST'], warnings=[], missed_opportunities=[], entry_gate={'allowed':True, 'reason':''})
        sched = pd.Series({'market_open':NOW-timedelta(hours=1), 'market_close':NOW+timedelta(hours=5)})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'
            state = fresh_state(NOW.isoformat()); state['version'] = 1
            for k in ('A40','A50','A60'):
                state['models'].pop(k)
            path.write_text(json.dumps(state), encoding='utf-8')
            with patch('auto_runner.datetime', Clock), patch('auto_runner.session', return_value=sched), \
                 patch('auto_runner.load_universe', return_value={'symbols':['TEST']}), \
                 patch('auto_runner.scan_pipeline', side_effect=scan), patch('auto_runner.time.time', return_value=EPOCH), \
                 patch('auto_runner.exact_quote', return_value={'bid':2.,'ask':2.2}):
                run(path, CFG)
                saved = json.loads(path.read_text())
                run(path, CFG)  # Same slot must not append another audit or trade.
            self.assertEqual(saved, json.loads(path.read_text()))
            self.assertEqual(saved['version'], VERSION)
            self.assertEqual(saved['models']['A']['cash'], 100199.)
            self.assertEqual(len(saved['last_audit']['execution']), 13)
            self.assertTrue((path.parent/saved['last_run']['audit_file']).exists())
            self.assertEqual(saved['last_audit']['path'][-1], 'execution')


if __name__ == '__main__':
    unittest.main()
