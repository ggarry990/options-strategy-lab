import copy
import json
import gzip
from pathlib import Path
import unittest
from unittest.mock import patch, Mock

import streamlit as st
import requests
from streamlit.testing.v1 import AppTest

from paper_core import fresh_state, run_cycle, BASE_STRATEGIES
from test_pipeline import NOW, candidate, entry, CFG
from dataclasses import asdict
from scan_recovery import record_retry


class DashboardTests(unittest.TestCase):
    def test_refresh_download_failure_keeps_last_successful_portfolios(self):
        state = fresh_state(NOW.isoformat())
        state['last_success'] = NOW.isoformat()
        state['models']['A']['cash'] = 98765.
        app = self.render(state)
        with patch('requests.get', side_effect=requests.Timeout('results download timed out')):
            next(b for b in app.button if b.label == 'Refresh results').click().run(timeout=20)
        self.assertFalse(list(app.exception))
        self.assertTrue(any('last successfully loaded results' in w.value for w in app.warning))
        results = next(r.value for r in app.dataframe if 'Available cash' in r.value.columns)
        self.assertEqual(results.loc['A · Hold to expiry', 'Available cash'], 98765.)
        self.assertEqual(app.session_state['last_valid_results'], state)

    def test_refresh_recovers_without_changing_saved_portfolios(self):
        state = fresh_state(NOW.isoformat())
        state['last_success'] = NOW.isoformat()
        before = copy.deepcopy(state)
        app = self.render(state)
        with patch('requests.get', side_effect=requests.Timeout('temporary outage')):
            next(b for b in app.button if b.label == 'Refresh results').click().run(timeout=20)
        latest = copy.deepcopy(state)
        latest['last_success'] = '2026-09-14T11:30:00-04:00'
        latest['models']['A']['cash'] = 100123.
        response = Mock(status_code=200)
        response.json.return_value = latest
        with patch('requests.get', return_value=response):
            next(b for b in app.button if b.label == 'Refresh results').click().run(timeout=20)
        self.assertFalse(list(app.exception))
        self.assertFalse(any('Refresh failed:' in w.value for w in app.warning))
        self.assertEqual(app.session_state['last_valid_results'], latest)
        self.assertEqual(state, before)

    def test_missing_or_invalid_refresh_never_replaces_last_valid_results(self):
        for status, payload in [(404, {}), (200, {'version':999,'models':{}}), (200, [])]:
            with self.subTest(status=status, payload=payload):
                state = fresh_state(NOW.isoformat())
                app = self.render(state)
                response = Mock(status_code=status)
                response.json.return_value = payload
                with patch('requests.get', return_value=response):
                    next(b for b in app.button if b.label == 'Refresh results').click().run(timeout=20)
                self.assertFalse(list(app.exception))
                self.assertTrue(any('last successfully loaded results' in w.value for w in app.warning))
                self.assertEqual(app.session_state['last_valid_results'], state)

    def test_initial_failure_does_not_invent_or_initialize_results(self):
        st.cache_data.clear()
        with patch('requests.get', side_effect=requests.Timeout('initial outage')):
            app = AppTest.from_file(str(Path(__file__).with_name('app.py'))).run(timeout=20)
        self.assertFalse(list(app.exception))
        self.assertTrue(any('No previously loaded results' in e.value for e in app.error))
        self.assertEqual(len(app.dataframe), 0)


    def render(self, state):
        st.cache_data.clear()
        response = Mock(status_code=200)
        response.json.return_value = state
        with patch('requests.get', return_value=response):
            app = AppTest.from_file(str(Path(__file__).with_name('app.py'))).run(timeout=20)
        self.assertFalse(list(app.exception), [x.message for x in app.exception])
        return app

    def test_legacy_results_render_before_migration(self):
        state = fresh_state(NOW.isoformat())
        state['version'] = 1
        for k in ('A40', 'A50', 'A60'):
            state['models'].pop(k)
        app = self.render(state)
        self.assertTrue(any('previous scanner' in m.value for m in app.info))
        rules = next(t.value for t in app.table if 'Portfolio status' in t.value.columns)
        self.assertEqual(rules.loc[rules['Strategy']=='A40', 'Portfolio status'].iloc[0], 'Waiting for first saved run')

    def test_expanded_results_and_diagnostics_render(self):
        state = fresh_state(NOW.isoformat())
        state['option_cache'] = {'TEST':entry([candidate()])}
        state['last_audit'] = dict(time=NOW.isoformat(), config=asdict(CFG), eligible_symbols=['TEST'],
            stage1=[{'Ticker':'TEST', 'Eligible':True}], stage2=[], stage3=[],
            rolling_ranking=[candidate()], contract_audit=[candidate()], portfolio_constraints=[])
        app = self.render(state)
        self.assertGreater(len(app.dataframe), 5)

    def test_compressed_audit_loads_with_full_ranking(self):
        st.cache_data.clear()
        state = fresh_state(NOW.isoformat())
        state['last_run'] = {'audit_file':'audits/test.json.gz'}
        audit = dict(time=NOW.isoformat(), rolling_ranking=[candidate()], portfolio_constraints=[],
                     config=asdict(CFG), summary={})
        state_response = Mock(status_code=200)
        state_response.json.return_value = state
        audit_response = Mock(content=gzip.compress(json.dumps(audit).encode()))
        with patch('requests.get', side_effect=lambda url, **kw: state_response if url.endswith('state.json') else audit_response):
            app = AppTest.from_file(str(Path(__file__).with_name('app.py'))).run(timeout=20)
        self.assertFalse(list(app.exception))
        self.assertFalse(list(app.error))

    def test_failed_coverage_and_retry_queue_are_visible(self):
        state=fresh_state(NOW.isoformat())
        state['last_run']=dict(status='New entries paused',entry_gate=dict(allowed=False,
            reason='Insufficient scan coverage',stage2_successful=67,stage2_planned=180))
        record_retry(state,'COST','stage2','Yahoo HTTP 401',NOW.timestamp())
        app=self.render(state)
        self.assertTrue(any('Insufficient scan coverage' in r.value for r in app.warning))
        queue=next(r.value for r in app.dataframe if 'next_retry_at' in r.value.columns)
        self.assertEqual(queue.iloc[0]['ticker'],'COST')

    def test_qualifying_count_is_visible_even_when_verification_is_paused(self):
        state=fresh_state(NOW.isoformat())
        state['last_run']=dict(qualifying_contracts=56, verified_contracts=0,
            coverage_blocked_contracts=56, selected_entries=0)
        app=self.render(state)
        metrics={m.label:m.value for m in app.metric}
        self.assertEqual(metrics['Qualifying before coverage gate'],'56')
        self.assertEqual(metrics['Verified entry contracts'],'0')
        self.assertEqual(metrics['Contracts blocked by coverage / access'],'56')

    def test_quality_warning_and_model_metrics_render(self):
        state = run_cycle(fresh_state(NOW.isoformat()),
            [candidate(verified_at=NOW.timestamp(), pre_gate_qualified=True)], {}, {}, NOW.isoformat(), 'one')
        state['last_run'] = dict(status='Completed with data warnings',
            entry_gate=dict(allowed=True, quality_warning='QUALITY WARNING: incomplete scan coverage'))
        app = self.render(state)
        self.assertTrue(any('QUALITY WARNING' in w.value for w in app.warning))
        table = next(r.value for r in app.dataframe if 'Open / limit' in r.value.columns).set_index('Model')
        self.assertEqual(table.loc['A_P10','Open / limit'], '1 / 10')
        self.assertEqual(table.loc['A_P10','Remaining slots'], 9)
        self.assertEqual(table.loc['A_P10','Executed opens'], 1)
        self.assertEqual(table.loc['A','Verified contracts'], 1)
        self.assertTrue(any('missed/delayed counts are unknown' in w.value for w in app.warning))

    def test_version_two_portfolios_render_without_initializing_variants(self):
        state = fresh_state(NOW.isoformat())
        state['version'] = 2
        state['models'] = {k:v for k,v in state['models'].items() if k in BASE_STRATEGIES}
        app = self.render(state)
        table = next(r.value for r in app.dataframe if 'Open / limit' in r.value.columns)
        self.assertEqual(len(table), 13)
        rules = next(t.value for t in app.table if 'Portfolio status' in t.value.columns)
        self.assertEqual(rules.loc[rules['Strategy']=='A_P8', 'Portfolio status'].iloc[0], 'Waiting for first saved run')


if __name__ == '__main__':
    unittest.main()
