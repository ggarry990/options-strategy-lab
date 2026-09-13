from __future__ import annotations
import json
from datetime import datetime, timezone
import pandas as pd
import requests
import streamlit as st
from paper_core import STRATEGIES, CAPITAL

st.set_page_config(page_title='Automated Options Lab', page_icon='🧪', layout='wide')
st.title('Automated Options Lab')
st.caption('Ten strategies • $100,000 each • S&P 500 + Nasdaq 100 • paper simulations only')
URL = 'https://raw.githubusercontent.com/ggarry990/options-strategy-lab/paper-results/state.json'

@st.cache_data(ttl=60, show_spinner=False)
def load_results():
    r = requests.get(URL, timeout=20, headers={'Cache-Control':'no-cache'})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    data = r.json()
    if data.get('version') != 1 or set(data.get('models', {})) != set(STRATEGIES):
        raise ValueError('Unexpected results format')
    return data

with st.sidebar:
    st.header('Automatic schedule')
    st.write('Every 30 minutes during US market hours, with an after-close settlement check.')
    st.caption('Target starts: :07 and :37. Exchange holidays, daylight saving and early closes are handled. GitHub may delay or skip scheduled runs.')
    st.link_button('Scheduler & run logs', 'https://github.com/ggarry990/options-strategy-lab/actions/workflows/paper.yml')
    if st.button('Refresh results', use_container_width=True):
        load_results.clear()
    st.divider()
    st.write('Each portfolio starts with $100,000. Maximum five positions, 20% per stock, at least 10% cash reserve.')
    st.caption('No brokerage connection. New experiment portfolios are separate from the original manual lab.')

try:
    state = load_results()
except Exception as exc:
    st.error(f'Cannot fetch saved results: {exc}. Portfolio values are unavailable; this does not reset the experiment.')
    st.stop()
if state is None:
    st.info('Waiting for the first scheduled run. No simulated trades have been created yet.')
    st.table(pd.DataFrame([dict(Strategy=k, Name=v['name'], Entry_Index=v['minimum']) for k,v in STRATEGIES.items()]))
    st.stop()

last = state.get('last_run', {})
stamp = state.get('last_success')
c1,c2,c3,c4 = st.columns(4)
c1.metric('Last run', last.get('status', 'Waiting'))
c2.metric('Index stocks screened', last.get('checked', 0))
c3.metric('Option stocks scanned', last.get('option_scanned', 0))
c4.metric('Eligible entry contracts', last.get('candidates', 0))
st.caption(f"Last saved: {stamp} • Started: {state['created']} • All times include their UTC offset.")
if stamp and (datetime.now(timezone.utc)-datetime.fromisoformat(stamp)).total_seconds() > 5400:
    st.warning('Results are more than 90 minutes old. Markets may be closed; check run logs if a scheduled market-hours update is missing.')
st.caption('Screening: every index stock; options: top 20 affordable/liquid stocks plus 20 rotating candidates. Candidates are re-quoted before simulated entry.')

overview, holdings, decisions, rules, health = st.tabs(['Results over time','Current holdings','Trades & decisions','Strategy rules','Data & schedule'])
with overview:
    rows, points = [], []
    for key,m in state['models'].items():
        nav = m['history'][-1]['nav'] if m['history'] else CAPITAL
        reserve = sum(p['strike']*100 for p in m['positions'])
        closed = m['closed']
        rows.append({'Strategy':key+' · '+STRATEGIES[key]['name'], 'Portfolio value':nav,'Total P&L':nav-CAPITAL,'Return %':100*(nav/CAPITAL-1),'Available cash':m['cash']-reserve,'Open positions':len(m['positions']),'Closed trades':len(closed),'Realized P&L':sum(t['pnl'] for t in closed),'Win rate %':100*sum(t['pnl']>0 for t in closed)/len(closed) if closed else None,'Max drawdown %':100*m['max_drawdown'],'Fees':m['fees'],'Stale marks':m['history'][-1]['stale'] if m['history'] else 0})
        points.append(dict(Time=state['created'],Strategy=key,Value=CAPITAL))
        points.extend(dict(Time=p['time'],Strategy=key,Value=p['nav']) for p in m['history'])
    st.dataframe(pd.DataFrame(rows).set_index('Strategy'), use_container_width=True)
    chart = pd.DataFrame(points)
    chart['Time'] = pd.to_datetime(chart['Time'], utc=True)
    st.line_chart(chart.pivot_table(index='Time',columns='Strategy',values='Value',aggfunc='last'))
    st.caption('Portfolio value includes open option liabilities at the ask and fees. A stale mark carries the last known value; it is never changed to zero. Drawdown uses these observed values.')
    if not any(m['closed'] for m in state['models'].values()):
        st.info('No completed trades yet. These are forward results, not a backtest or a proven ranking.')
with holdings:
    key = st.selectbox('Portfolio',list(STRATEGIES),format_func=lambda k:k+' · '+STRATEGIES[k]['name'])
    rows = []
    for p in state['models'][key]['positions']:
        rows.append({'Stock':p['ticker'],'Contract':p['contract'],'Expiry':p['expiry'],'Strike':p['strike'],'Entry credit':p['credit'],'Buyback mark':p['mark'],'Open P&L (before exit fee)':(p['credit']-p['mark'])*100-1,'Premium captured %':100*(1-p['mark']/p['credit']),'Entry Index':p['score'],'Entered':p['entered'],'Mark time':p['mark_time'],'Stale':p['mark_time']!=stamp})
    if rows:
        st.dataframe(pd.DataFrame(rows),use_container_width=True,hide_index=True)
    else:
        st.info('No open holdings. Cash stays undeployed until entry rules are met.')
with decisions:
    key2=st.selectbox('Strategy history',list(STRATEGIES),format_func=lambda k:k+' · '+STRATEGIES[k]['name'])
    m=state['models'][key2]
    st.subheader('Closed trades')
    st.dataframe(pd.DataFrame(m['closed']),use_container_width=True,hide_index=True)
    st.subheader('Recent decisions')
    st.dataframe(pd.DataFrame(list(reversed(m['events']))[:500]),use_container_width=True,hide_index=True)
    st.download_button('Download all portfolios & history',json.dumps(state,indent=2),file_name='automated_paper_results.json',mime='application/json')
with rules:
    st.table(pd.DataFrame([{'Strategy':k,'Rule':v['name'],'Minimum entry Index':v['minimum']} for k,v in STRATEGIES.items()]))
    st.write('All strategies sell one cash-secured put per stock, 21–60 days to expiry, with $3,000–$20,000 collateral per contract. The existing Opportunity Index uses 30% return / 70% protection. Entries require positive bids, a spread at most 25% of ask, at least 100 open interest, a trade today, and a known earnings date outside the holding window.')
    st.write('Entry sells at bid; early exit buys at ask; fee is $1 per contract per side. A stock closed this cycle cannot reopen until a later cycle. Loss limits are checked periodically, so losses can exceed the threshold between runs.')
    st.write('G closes a profitable put when captured premium is at least 35 percentage points above an entry-spot, entry-volatility Black–Scholes decay baseline. This is a model estimate, not guaranteed income.')
    st.write('At expiry, puts use cash-equivalent intrinsic settlement at the expiry session’s unadjusted stock close. This experiment does not model physical assignment, covered calls, dividends, interest, early assignment or exercise fees. It differs from a full wheel portfolio.')
with health:
    st.json(last)
    st.write('Universe source', state.get('universe',{}).get('source','Not loaded yet'))
    st.dataframe(pd.DataFrame(list(reversed(state.get('runs',[])))),use_container_width=True,hide_index=True)
    st.warning('Yahoo data may be delayed, unavailable or throttled. A trade today is a liquidity check, not a live bid/ask timestamp guarantee. Missing data pauses affected trades and appears in the log.')
    st.caption('Results live on the paper-results branch and survive restarts. Only the scheduled runner changes portfolios. Loading this page never creates trades. Rules stay fixed during this experiment.')
