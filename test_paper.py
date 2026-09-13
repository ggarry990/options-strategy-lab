import unittest
from datetime import date
from paper_core import fresh_state, run_cycle, exit_reason, STRATEGIES, expected_capture

NOW='2026-09-14T10:00:00-04:00'
LATER='2026-09-14T10:30:00-04:00'
def candidate(ticker='TEST',score=130):
    return dict(ticker=ticker,contract=ticker+'261016P00100000',strike=100.,expiry='2026-10-16',dte=32,bid=2.,ask=2.2,score=score,iv=.3,spot=110.)

class PaperTests(unittest.TestCase):
    def opened(self):
        return run_cycle(fresh_state(NOW),[candidate()],{},{},NOW,'one')

    def test_entry_accounts_for_spread_and_fee(self):
        m=self.opened()['models']['A']
        self.assertEqual(m['cash'],100199.)
        self.assertEqual(m['history'][-1]['nav'],99979.)
        self.assertEqual(m['fees'],1)

    def test_duplicate_run_is_idempotent(self):
        s=self.opened()
        self.assertEqual(s,run_cycle(s,[candidate()],{},{},NOW,'one'))

    def test_missing_quote_carries_liability(self):
        s=run_cycle(self.opened(),[],{},{},LATER,'two')
        self.assertEqual(s['models']['C']['history'][-1]['nav'],99979.)
        self.assertEqual(s['models']['C']['history'][-1]['stale'],1)
        self.assertFalse(s['models']['C']['closed'])

    def test_profit_exit_and_no_same_cycle_reentry(self):
        s=run_cycle(self.opened(),[candidate()],{candidate()['contract']:{'ask':1.}},{},LATER,'two')
        m=s['models']['C']
        self.assertEqual(m['closed'][0]['pnl'],98.)
        self.assertEqual(m['history'][-1]['nav'],100098.)
        self.assertFalse(m['positions'])
        self.assertTrue(s['models']['A']['positions'])

    def test_expiry_cash_settlement(self):
        s=run_cycle(self.opened(),[],{}, {candidate()['contract']:90.},'2026-10-16T16:37:00-04:00','expiry',False)
        m=s['models']['A']
        self.assertEqual(m['closed'][0]['pnl'],-801.)
        self.assertEqual(m['cash'],99199.)

    def test_expiry_waits_for_actual_close(self):
        s=run_cycle(self.opened(),[],{},{},'2026-10-17T10:00:00-04:00','expiry',False)
        self.assertTrue(s['models']['A']['positions'])
        self.assertFalse(s['models']['A']['closed'])

    def test_capacity_and_one_per_stock(self):
        rows=[candidate(str(i)) for i in range(20)]+[candidate('0')]
        m=run_cycle(fresh_state(NOW),rows,{},{},NOW,'one')['models']['A']
        self.assertEqual(len(m['positions']),5)
        self.assertEqual(len({p['ticker'] for p in m['positions']}),5)
        self.assertGreaterEqual(m['cash']-sum(p['strike']*100 for p in m['positions']),10000)

    def test_selective_entry_thresholds(self):
        s=run_cycle(fresh_state(NOW),[candidate(score=90)],{},{},NOW,'one')
        self.assertTrue(s['models']['A']['positions'])
        for k in ['H','I','J']:
            self.assertFalse(s['models'][k]['positions'])

    def test_time_stop_and_ahead_rules(self):
        p=self.opened()['models']['G']['positions'][0]
        self.assertIsNotNone(exit_reason(STRATEGIES['F'],p,6.,date(2026,9,14)))
        self.assertIsNotNone(exit_reason(STRATEGIES['E'],p,2.,date(2026,10,9)))
        self.assertIsNotNone(exit_reason(STRATEGIES['G'],p,1.,date(2026,9,14)))
        self.assertAlmostEqual(expected_capture(p,date(2026,9,14)),0.)
        self.assertIsNone(exit_reason(STRATEGIES['A'],p,20.,date(2026,9,14)))

    def test_market_closed_no_entries(self):
        s=run_cycle(fresh_state(NOW),[candidate()],{},{},NOW,'one',False)
        self.assertFalse(s['models']['A']['positions'])

if __name__=='__main__':
    unittest.main()
