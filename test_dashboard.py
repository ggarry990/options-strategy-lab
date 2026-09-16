import copy
import json
import gzip
from pathlib import Path
import unittest
from unittest.mock import patch, Mock

import streamlit as st
from streamlit.testing.v1 import AppTest

from paper_core import fresh_state
from test_pipeline import NOW, candidate, entry, CFG
from dataclasses import asdict
from scan_recovery import record_retry


class DashboardTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
