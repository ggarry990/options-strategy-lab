import copy
import gzip
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import pandas as pd

from engine import scan_put_ticker
from yahoo_options import YahooOptions, OptionDataError
from scan_recovery import coverage_history, prioritize_blind_spots, record_retry
from scan_progress import Progress, progress_view, publish
from test_reliability import response, EXPIRY
from test_pipeline import NOW, EPOCH


def empty_response(missing=False, both=False):
    payload = response().json()
    block = payload['optionChain']['result'][0]['options'][0]
    if missing:
        block.pop('puts')
    else:
        block['puts'] = []
    if both:
        block['calls'] = []
    return Mock(status_code=200, json=lambda: payload)


class ExpiryRecoveryTests(TestCase):
    def reader(self, replies, state=None, **kwargs):
        data = Mock(get=Mock(side_effect=replies))
        reader = YahooOptions(state or {}, spacing=0, **kwargs)
        reader.tickers['TEST'] = SimpleNamespace(_data=data)
        return reader, data

    def test_empty_puts_require_two_network_observations(self):
        reader, data = self.reader([empty_response(), empty_response()])
        chain = reader.chain('TEST', EXPIRY)
        self.assertEqual(chain.put_status, 'empty_confirmed')
        self.assertEqual(data.get.call_count, 2)
        self.assertIs(chain, reader.chain('TEST', EXPIRY))
        self.assertEqual(data.get.call_count, 2)

    def test_puts_return_on_confirmation(self):
        reader, data = self.reader([empty_response(), response()])
        self.assertEqual(reader.chain('TEST', EXPIRY).put_status, 'available')
        self.assertEqual(data.get.call_count, 2)

    def test_missing_side_and_both_empty_remain_unavailable(self):
        for reply in (empty_response(missing=True), empty_response(both=True)):
            reader, _ = self.reader([reply])
            with self.assertRaises(OptionDataError):
                reader.chain('TEST', EXPIRY)

    def test_confirmation_failure_does_not_become_empty_success(self):
        reader, data = self.reader([empty_response(), response(401)])
        for _ in range(2):
            with self.assertRaises(OptionDataError):
                reader.chain('TEST', EXPIRY)
        self.assertEqual(data.get.call_count, 2)
        self.assertEqual(reader.chains[('TEST', EXPIRY)].put_status, 'empty_unconfirmed')

    def test_engine_audits_confirmed_empty_without_failure_warning(self):
        reader, _ = self.reader([empty_response(), empty_response()])
        audit = []
        with reader, patch('engine.get_stock_price', return_value=110.), \
             patch('engine.get_historical_volatility', return_value=.3), \
             patch('engine.get_earnings_dates', return_value=[]):
            frame, warnings = scan_put_ticker('TEST',21,60,3000,20000,0,
                asof=NOW.date(), expiry_audit=audit)
        self.assertTrue(frame.empty)
        self.assertEqual(warnings, [])
        self.assertEqual(audit[0]['status'], 'empty_confirmed')

    def test_engine_failure_keeps_expiry_reason(self):
        reader, _ = self.reader([response(), response(401)])
        reader.expirations('TEST')
        reader.chains.clear()
        with reader, patch('engine.get_stock_price', return_value=110.), \
             patch('engine.get_historical_volatility', return_value=.3), \
             patch('engine.get_earnings_dates', return_value=[]):
            audit = []
            _, warnings = scan_put_ticker('TEST',21,60,3000,20000,0,
                asof=NOW.date(), expiry_audit=audit)
        self.assertTrue(warnings)
        self.assertEqual(audit[0]['status'], 'unavailable')
        self.assertIn('401', audit[0]['reason'])

    def test_persisted_success_reused_only_while_fresh(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'chains.gz'
            with patch('yahoo_options.time.time', return_value=EPOCH):
                reader, _ = self.reader([response()], cache_path=path)
                with reader:
                    reader.chain('TEST', EXPIRY)
            with patch('yahoo_options.time.time', return_value=EPOCH+120):
                next_reader, data = self.reader([], cache_path=path)
                self.assertEqual(next_reader.chain('TEST', EXPIRY).observed_at, EPOCH)
                data.get.assert_not_called()
            with patch('yahoo_options.time.time', return_value=EPOCH+1801):
                next_reader, data = self.reader([response(401)], cache_path=path)
                with self.assertRaises(OptionDataError):
                    next_reader.chain('TEST', EXPIRY)
                self.assertEqual(data.get.call_count, 1)

    def test_verification_still_refreshes_older_than_45_seconds(self):
        with patch('yahoo_options.time.time', return_value=EPOCH):
            reader, data = self.reader([response(), response(401)])
            reader.chain('TEST', EXPIRY)
        with patch('yahoo_options.time.time', return_value=EPOCH+46):
            with self.assertRaises(OptionDataError):
                reader.chain('TEST', EXPIRY, max_age=45)
        self.assertEqual(data.get.call_count, 2)

    def test_retry_requests_missing_expiry_only_when_other_expiry_is_fresh(self):
        other = '2026-10-23'
        fixed = response().json()
        fixed['optionChain']['result'][0]['options'][0]['expirationDate'] = int(pd.Timestamp(other, tz='UTC').timestamp())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'chains.gz'
            with patch('yahoo_options.time.time', return_value=EPOCH):
                reader, _ = self.reader([response(), response(401)], cache_path=path)
                with reader:
                    reader.chain('TEST', EXPIRY)
                    with self.assertRaises(OptionDataError):
                        reader.chain('TEST', other)
            with patch('yahoo_options.time.time', return_value=EPOCH+300):
                reader, data = self.reader([Mock(status_code=200, json=lambda: fixed)], cache_path=path)
                self.assertEqual(reader.chain('TEST', EXPIRY).observed_at, EPOCH)
                reader.chain('TEST', other)
                self.assertEqual(data.get.call_count, 1)
                self.assertEqual(data.get.call_args.kwargs['params']['date'], int(pd.Timestamp(other, tz='UTC').timestamp()))

    def test_corrupt_disposable_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'chains.gz'
            for content in (b'broken', gzip.compress(b'[]'), gzip.compress(b'{}')[:5]):
                path.write_bytes(content)
                self.assertEqual(YahooOptions({}, cache_path=path).chains, {})

    def test_valid_metadata_survives_unusable_default_chain(self):
        reader, data = self.reader([empty_response(both=True), response()])
        self.assertEqual(reader.expirations('TEST'), [EXPIRY])
        self.assertEqual(reader.chain('TEST', EXPIRY).put_status, 'available')
        self.assertEqual(data.get.call_count, 2)


class CoverageHistoryTests(TestCase):
    def test_oldest_and_never_scanned_prioritized_without_ignoring_backoff(self):
        state = {'option_cache':{
            'OLD':dict(status='partial', last_complete_at=EPOCH-7200),
            'RECENT':dict(status='complete', scanned_at=EPOCH-60)}}
        record_retry(state,'WAIT','stage3','missing',EPOCH)
        before = copy.deepcopy(state)
        names = ['OLD','RECENT','NEVER','WAIT']
        self.assertEqual(prioritize_blind_spots(state,names,20,EPOCH,30), ['NEVER','OLD'])
        rows = coverage_history(state,names,EPOCH,30)
        self.assertEqual(next(r for r in rows if r['ticker']=='OLD')['age_minutes'],120)
        self.assertEqual(state,before)


class ProgressTests(TestCase):
    def test_atomic_progress_has_no_portfolio_content(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'GITHUB_RUN_ID':'123'}):
            path = Path(directory)/'progress.json'
            Progress(path)('Stage 3', completed=5,total=120,ticker='COST')
            row = json.loads(path.read_text())
            self.assertEqual(row['run_id'],'123')
            self.assertEqual(row['completed'],5)
            self.assertNotIn('models',row)
            self.assertFalse(path.with_suffix('.tmp').exists())

    def test_old_progress_cannot_label_new_run(self):
        old = dict(run_id='1',updated_at=NOW.isoformat(),stage='Stage 3')
        label, detail = progress_view(old,dict(id=2,status='in_progress'),NOW)
        self.assertIsNone(detail)
        self.assertNotIn('Stage 3',label)

    def test_delayed_heartbeat_queued_and_failed(self):
        row = dict(run_id='1',updated_at=(NOW-timedelta(minutes=4)).isoformat(),stage='Stage 2')
        label, detail = progress_view(row,dict(id=1,status='in_progress'),NOW)
        self.assertIn('delayed',label)
        self.assertIs(detail,row)
        self.assertIn('queued',progress_view(row,dict(status='queued'),NOW)[0])
        self.assertIn('failure',progress_view(row,dict(status='completed',conclusion='failure'),NOW)[0])

    def test_publisher_targets_only_progress_branch(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'GITHUB_RUN_ID':'123'}):
            path = Path(directory)/'progress.json'
            Progress(path)('Stage 2')
            with patch('scan_progress.subprocess.run', return_value=SimpleNamespace(stdout='')) as git:
                publish(path)
            commands = [call.args[0] for call in git.call_args_list]
            self.assertEqual(commands[-1][-1], ':refs/heads/scan-progress')
            self.assertFalse(any('paper-results' in str(c) or 'checkout' in c for c in commands))
