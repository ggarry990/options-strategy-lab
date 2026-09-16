import copy
import json
import time
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import pandas as pd

from yahoo_options import YahooOptions, OptionDataError, ACTIVE
from scan_recovery import (record_retry, clear_retry, due_retries, retry_waiting,
                           seed_previous_failures, coverage_gate)
from pipeline import ScanConfig, scan_pipeline
from engine import scan_put_ticker
from paper_core import fresh_state, run_cycle
from test_pipeline import EPOCH, NOW, candidate, option_row, entry
from auto_runner import run

EXPIRY = '2026-10-16'
EXPIRY_TS = int(pd.Timestamp(EXPIRY, tz='UTC').timestamp())


def response(status=200, blocks=True):
    contract = dict(strike=100., bid=2., ask=2.2, impliedVolatility=.4,
        contractSymbol='TEST-P100', openInterest=500, lastTradeDate=int(EPOCH))
    root = dict(expirationDates=[EXPIRY_TS], options=[dict(expirationDate=EXPIRY_TS,
                calls=[contract], puts=[contract])] if blocks else [])
    return Mock(status_code=status, json=lambda: {'optionChain':{'result':[root]}})


class YahooReaderTests(TestCase):
    def test_reuses_session_chain_and_original_timestamp(self):
        state = {}
        data = Mock(get=Mock(return_value=response()))
        with patch('yahoo_options.yf.Ticker', return_value=SimpleNamespace(_data=data)), \
             patch('yahoo_options.time.time', return_value=EPOCH):
            with YahooOptions(state, spacing=0) as reader:
                self.assertEqual(reader.expirations('TEST'), [EXPIRY])
                first = reader.chain('TEST', EXPIRY)
                second = reader.chain('TEST', EXPIRY)
                self.assertIs(first, second)
                self.assertEqual(first.observed_at, EPOCH)
                self.assertEqual(data.get.call_count, 1)
                self.assertEqual(str(first.puts.iloc[0]['lastTradeDate'].tz), 'UTC')
                self.assertNotIn('crumb', data.get.call_args.kwargs['params'])
            self.assertIsNone(ACTIVE.get())
            with YahooOptions(state, spacing=0) as next_run:
                self.assertEqual(next_run.expirations('TEST'), [EXPIRY])
                self.assertEqual(data.get.call_count, 1)  # Persisted expiry metadata.
        json.dumps(state, allow_nan=False)

    def test_three_access_failures_pause_and_do_not_retry_every_ticker(self):
        data = Mock(get=Mock(return_value=response(401)))
        state = {}
        with patch('yahoo_options.yf.Ticker', return_value=SimpleNamespace(_data=data)), \
             patch('yahoo_options.time.time', return_value=EPOCH), \
             patch('yahoo_options.time.sleep'):
            with YahooOptions(state) as reader:
                for symbol in ('A', 'B', 'C', 'D'):
                    with self.assertRaises(OptionDataError):
                        reader.expirations(symbol)
                self.assertEqual(data.get.call_count, 3)
                self.assertTrue(reader.paused)
                with self.assertRaises(OptionDataError):
                    reader.expirations('A')
                self.assertEqual(data.get.call_count, 3)
            with YahooOptions(state) as restarted:
                self.assertTrue(restarted.paused)
        with patch('yahoo_options.time.time', return_value=EPOCH+901):
            self.assertFalse(YahooOptions(state).paused)

    def test_rate_limit_and_sensitive_exception_are_safely_reported(self):
        class YFRateLimitError(Exception):
            pass
        data = Mock(get=Mock(side_effect=YFRateLimitError('secret crumb=PRIVATE')))
        with patch('yahoo_options.yf.Ticker', return_value=SimpleNamespace(_data=data)):
            with self.assertRaises(OptionDataError) as caught:
                YahooOptions({}, spacing=0).expirations('A')
        self.assertEqual(caught.exception.category, 'rate_limit')
        self.assertNotIn('PRIVATE', str(caught.exception))

    def test_expired_chain_cannot_be_refreshed_by_empty_response(self):
        data = Mock(get=Mock(side_effect=[response(), response(blocks=False)]))
        with patch('yahoo_options.yf.Ticker', return_value=SimpleNamespace(_data=data)), \
             patch('yahoo_options.time.time', return_value=EPOCH):
            reader = YahooOptions({}, spacing=0)
            original = reader.chain('TEST', EXPIRY)
        with patch('yahoo_options.time.time', return_value=EPOCH+60):
            with self.assertRaises(OptionDataError):
                reader.chain('TEST', EXPIRY, max_age=45)
            self.assertEqual(original.observed_at, EPOCH)

    def test_pacing_between_network_requests(self):
        data = Mock(get=Mock(return_value=response()))
        with patch('yahoo_options.yf.Ticker', return_value=SimpleNamespace(_data=data)), \
             patch('yahoo_options.time.monotonic', return_value=100), patch('yahoo_options.time.sleep') as sleep:
            reader = YahooOptions({}, spacing=.75)
            reader.expirations('A'); reader.expirations('B')
            sleep.assert_called_once_with(.75)

    def test_engine_keeps_chain_observation_time(self):
        data = Mock(get=Mock(return_value=response()))
        with patch('yahoo_options.yf.Ticker', return_value=SimpleNamespace(_data=data)), \
             patch('yahoo_options.time.time', return_value=EPOCH):
            reader = YahooOptions({}, spacing=0)
            reader.expirations('TEST')
        with reader, patch('yahoo_options.time.time', return_value=EPOCH+100), \
             patch('engine.get_stock_price', return_value=110.), \
             patch('engine.get_historical_volatility', return_value=.3), \
             patch('engine.get_earnings_dates', return_value=[]):
            frame, warnings = scan_put_ticker('TEST',21,60,3000,20000,0,asof=NOW.date())
        self.assertFalse(warnings)
        self.assertEqual(frame.iloc[0]['Observed At'], EPOCH)


class RecoveryTests(TestCase):
    def test_backoff_survives_save_and_only_success_clears_it(self):
        state = {}
        record_retry(state, 'A', 'stage2', 'HTTP 401', EPOCH)
        record_retry(state, 'A', 'stage2', 'deferred', EPOCH+1, attempted=False)
        state = json.loads(json.dumps(state))
        self.assertEqual(state['scan_retries']['stage2:A']['error'], 'HTTP 401')
        self.assertTrue(retry_waiting(state, 'A','stage2',EPOCH+60))
        self.assertEqual(due_retries(state,'stage2',{'A'},EPOCH+1800,40),['A'])
        record_retry(state,'A','stage2','HTTP 401',EPOCH+1800)
        self.assertEqual(state['scan_retries']['stage2:A']['next_retry_at'],EPOCH+5400)
        clear_retry(state,'A','stage2')
        self.assertFalse(state['scan_retries'])

    def test_seeds_existing_failures_without_changing_portfolios(self):
        state = fresh_state(NOW.isoformat())
        original = copy.deepcopy(state['models'])
        state['last_audit'] = {'stage2':[dict(ticker='A',status='unavailable',error='HTTP 401')]}
        seed_previous_failures(state,EPOCH)
        self.assertEqual(due_retries(state,'stage2',{'A'},EPOCH,40),['A'])
        clear_retry(state,'A','stage2'); seed_previous_failures(state,EPOCH+30)
        self.assertFalse(state['scan_retries'])
        self.assertEqual(state['models'],original)

    def test_seeds_last_scan_failures_after_an_after_close_save(self):
        state = dict(last_audit={'stage2':[]}, runs=[
            {'stage2_checked':180,'warnings':['COST ATM: Yahoo HTTP 401', 'Position warning']},
            {'stage2_checked':0,'warnings':[]}])
        seed_previous_failures(state,EPOCH)
        self.assertEqual(due_retries(state,'stage2',{'COST'},EPOCH,40),['COST'])

    def test_gate_counts_deferred_names_not_only_attempts(self):
        report = dict(stage2_planned=list(range(180)),stage3_planned=list(range(100)),
            stage2=[dict(status='checked')]*67,stage3=[dict(status='complete')]*100)
        gate = coverage_gate(report,ScanConfig())
        self.assertFalse(gate['allowed'])
        self.assertEqual(gate['stage2_ratio'],67/180)
        report['stage2'] = [dict(status='checked')]*162
        self.assertTrue(coverage_gate(report,ScanConfig())['allowed'])
        report['stage3'] = [dict(status='complete')]*89
        self.assertFalse(coverage_gate(report,ScanConfig())['allowed'])

    def test_failed_stock_gets_reserved_slot_and_recovers(self):
        symbols = [f'S{i:03}' for i in range(220)]
        pre = pd.DataFrame([dict(Ticker=s,Eligible=True,**{'Price Date':NOW.isoformat(), 'Stock Price':110., 'HV30':.3}) for s in symbols])
        state = {}
        record_retry(state,'S219','stage2','HTTP 401',EPOCH-1800)
        with patch('pipeline.prescreen_underlyings',return_value=(pre,[])), \
             patch('pipeline.atm_snapshot',side_effect=lambda r,t: dict(ticker=r['Ticker'],status='checked',prescreen_score=2)), \
             patch('pipeline.scan_put_ticker',side_effect=lambda s,*a,**kw: (pd.DataFrame([option_row(s)]),[])), \
             patch('pipeline.time.time',return_value=EPOCH):
            result=scan_pipeline(state,{'symbols':symbols},NOW,ScanConfig(),time.monotonic())
        self.assertEqual(result['stage2_planned'][0],'S219')
        self.assertNotIn('stage2:S219',state['scan_retries'])
        self.assertTrue(result['entry_gate']['allowed'])

    def test_cooldown_queues_unattempted_stocks_and_blocks_entry(self):
        symbols = ['A','B','C','D','E']
        pre = pd.DataFrame([dict(Ticker=s,Eligible=True,**{'Price Date':NOW.isoformat(), 'Stock Price':110., 'HV30':.3}) for s in symbols])
        data=Mock(get=Mock(return_value=response(401)))
        state={}
        with patch('yahoo_options.yf.Ticker',return_value=SimpleNamespace(_data=data)), \
             patch('pipeline.prescreen_underlyings',return_value=(pre,[])), \
             patch('pipeline.time.time',return_value=EPOCH), patch('yahoo_options.time.sleep'), \
             YahooOptions(state) as reader:
            result=scan_pipeline(state,{'symbols':symbols},NOW,ScanConfig(),time.monotonic())
        self.assertEqual(len(result['stage2']),3)
        self.assertEqual(data.get.call_count,3)
        self.assertFalse(result['entry_gate']['allowed'])
        self.assertEqual(set(result['stage2_deferred']),{'D','E'})
        self.assertEqual(state['scan_retries']['stage2:D']['attempts'],0)

    def test_blocked_entries_still_manage_existing_positions(self):
        state=run_cycle(fresh_state(NOW.isoformat()),[candidate()],{},{},NOW.isoformat(),'first')
        result=run_cycle(state,[candidate('NEW')],{candidate()['contract']:{'ask':1.}}, {},
            NOW.isoformat(),'second',False,'Scan coverage below minimum')
        self.assertTrue(result['models']['C']['closed'])
        self.assertFalse(any(p['ticker']=='NEW' for m in result['models'].values() for p in m['positions']))
        self.assertTrue(all('Scan coverage below minimum' in r['reasons'] for r in result['entry_audit']))

    def test_runner_persists_blocked_coverage_and_still_applies_exits(self):
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW
        state=run_cycle(fresh_state(NOW.isoformat()),[candidate()],{},{},NOW.isoformat(),'2026-09-14T10:30')
        state['option_cache']={'NEW':entry([candidate('NEW')])}
        audit=dict(stage1=[],stage2=[],stage3=[],eligible_symbols=['NEW'],warnings=[],
            entry_gate=dict(allowed=False,reason='Insufficient planned scan coverage'))
        sched=pd.Series({'market_open':NOW-timedelta(hours=1),'market_close':NOW+timedelta(hours=5)})
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.json'
            path.write_text(json.dumps(state),encoding='utf-8')
            with patch('auto_runner.datetime',Clock), patch('auto_runner.session',return_value=sched), \
                 patch('auto_runner.load_universe',return_value={'symbols':['NEW']}), \
                 patch('auto_runner.scan_pipeline',return_value=audit), \
                 patch('auto_runner.time.time',return_value=EPOCH), \
                 patch('auto_runner.exact_quote',return_value={'bid':.9,'ask':1.}) as quote:
                run(path,ScanConfig())
            saved=json.loads(path.read_text())
        self.assertFalse(saved['last_run']['entry_gate']['allowed'])
        self.assertEqual(saved['last_run']['qualifying_contracts'], 1)
        self.assertEqual(saved['last_run']['coverage_blocked_contracts'], 1)
        self.assertEqual(saved['last_run']['verified_contracts'], 0)
        self.assertTrue(saved['models']['C']['closed'])
        self.assertFalse(any(p['ticker']=='NEW' for m in saved['models'].values() for p in m['positions']))
        self.assertTrue(all(call.args[0]['ticker']=='TEST' for call in quote.call_args_list))
