from __future__ import annotations
import json
import gzip
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import pandas as pd
import requests
import streamlit as st
from paper_core import STRATEGIES, CAPITAL, VERSION
from pipeline import ScanConfig, rolling_ranking
from scan_schedule import next_events
from scan_progress import progress_view
from scan_recovery import coverage_history

st.set_page_config(page_title='Automated Options Lab', page_icon='🧪', layout='wide')
st.title('Automated Options Lab')
st.caption(f'{len(STRATEGIES)} strategies • $100,000 each • S&P 500 + Nasdaq 100 • paper simulations only')
URL = 'https://raw.githubusercontent.com/ggarry990/options-strategy-lab/paper-results/state.json'


@st.cache_data(ttl=60, show_spinner=False)
def load_live_status():
    workflow, progress = {}, {}
    try:
        response = requests.get('https://api.github.com/repos/ggarry990/options-strategy-lab/actions/workflows/paper.yml/runs',
            params={'per_page':1}, timeout=8)
        response.raise_for_status()
        runs = response.json().get('workflow_runs', [])
        workflow = runs[0] if isinstance(runs, list) and runs and isinstance(runs[0], dict) else {}
    except Exception:
        pass
    try:
        response = requests.get('https://raw.githubusercontent.com/ggarry990/options-strategy-lab/scan-progress/progress.json',
            params={'refresh':int(datetime.now(timezone.utc).timestamp())//60}, timeout=8)
        response.raise_for_status()
        payload = response.json()
        progress = payload if isinstance(payload, dict) else {}
    except Exception:
        pass
    return workflow, progress


@st.fragment(run_every='60s')
def show_live_status():
    workflow, progress = load_live_status()
    now = datetime.now(timezone.utc)
    label, detail = progress_view(progress, workflow, now)
    st.write(label)
    if detail:
        total, completed = detail.get('total', 0), detail.get('completed', 0)
        if total:
            st.progress(min(1., completed/total), text=f'{completed}/{total} processed')
        st.caption(f"Last progress update: {detail['updated_at']} • Current stock: {detail.get('ticker') or '—'}")
    if workflow.get('status') == 'completed' and workflow.get('updated_at'):
        expected = next_events(datetime.fromisoformat(workflow['updated_at'].replace('Z', '+00:00')))['scan']
        if expected and (now-expected).total_seconds() > 600:
            st.warning('A scheduled scan is overdue; no newer automation is confirmed. Check scheduler logs.')
    st.caption('Automation status refreshes every minute. Stage progress is published about once a minute; detailed tables show the last saved run.')

@st.cache_data(ttl=60, show_spinner=False)
def load_results():
    r = requests.get(URL, timeout=20, headers={'Cache-Control':'no-cache'},
        params={'refresh':int(datetime.now(timezone.utc).timestamp())//60})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    data = r.json()
    if data.get('version') not in (1, VERSION) or not set('ABCDEFGHIJ').issubset(data.get('models', {})) or set(data.get('models', {}))-set(STRATEGIES):
        raise ValueError('Unexpected results format')
    return data


@st.fragment(run_every='60s')
def refresh_when_results_change(saved_at):
    try:
        latest = load_results()
    except Exception:
        return  # Keep the last visible results during a temporary refresh failure.
    if latest and latest.get('last_success') != saved_at:
        st.rerun()


@st.fragment(run_every='30s')
def show_next_scan(display_timezone):
    now = datetime.now(timezone.utc)
    events = next_events(now)
    target = events['scan']
    if target:
        seconds = max(0, int((target-now).total_seconds()))
        days, rest = divmod(seconds, 86400)
        hours, rest = divmod(rest, 3600)
        minutes = rest//60
        countdown = (f'{days}d ' if days else '')+f'{hours}h {minutes}m'
        st.metric('Next scheduled scan', target.astimezone(ZoneInfo(display_timezone)).strftime('%a %b %d, %I:%M %p %Z'))
        st.write(f'About {countdown} from now')
    else:
        st.warning('Next scan time is unavailable from the exchange calendar.')
    if events['settlement']:
        st.caption('Next after-close settlement check: '+events['settlement'].astimezone(ZoneInfo(display_timezone)).strftime('%a %b %d, %I:%M %p %Z'))
    st.caption('Expected schedule, not a confirmed start. GitHub may delay or skip runs; a long-running scan delays the next one. Weekends, exchange holidays and early closes are accounted for.')


@st.cache_data(show_spinner=False)
def load_audit(filename):
    if not filename.startswith('audits/') or '..' in filename:
        raise ValueError('Invalid audit path')
    response = requests.get(URL.rsplit('/', 1)[0]+'/'+filename, timeout=30)
    response.raise_for_status()
    payload = response.content
    if payload.startswith(b'\x1f\x8b'):
        payload = gzip.decompress(payload)
    return json.loads(payload)

with st.sidebar:
    st.header('Automatic schedule')
    st.write('Automated every 30 minutes during US market hours. This runs on GitHub even when this page and your computer are closed.')
    st.caption('Target minutes: :07 and :37. The slower cadence reduces Yahoo request pressure. Runs cannot overlap. After-close runs check settlements rather than scanning new opportunities.')
    display_timezone = st.selectbox('Schedule timezone', ['America/New_York', 'America/Edmonton', 'UTC'])
    st.link_button('Scheduler & run logs', 'https://github.com/ggarry990/options-strategy-lab/actions/workflows/paper.yml')
    if st.button('Refresh results', use_container_width=True):
        load_results.clear()
    st.divider()
    st.write('Each portfolio starts with $100,000. Maximum five positions, 20% per stock, at least 10% cash reserve.')
    st.caption('No brokerage connection. New experiment portfolios are separate from the original manual lab.')

show_next_scan(display_timezone)
show_live_status()

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
refresh_when_results_change(stamp)
if state.get('version') == 1:
    st.info('The updated scanner is installed, but the page is still showing results from the previous scanner. The new strategies and scan audit will appear after the first updated run saves. Results refresh automatically every minute.')
else:
    st.caption('Saved results refresh automatically every minute. Detailed scan tables update after a run finishes and saves.')
c1,c2,c3,c4 = st.columns(4)
c1.metric('Last run', last.get('status', 'Waiting'))
c2.metric('Index stocks screened', last.get('checked', 0))
c3.metric('Option stocks scanned', last.get('option_scanned', 0))
c4.metric('Verified entry contracts', last.get('verified_contracts', last.get('candidates', 0)))
if 'qualifying_contracts' in last:
    a, b, c = st.columns(3)
    a.metric('Qualifying before coverage gate', last['qualifying_contracts'])
    b.metric('Contracts blocked by coverage / access', last.get('coverage_blocked_contracts', 0))
    c.metric('Selected portfolio entries', last.get('selected_entries', 0))
    st.caption('Qualifying contracts pass data and contract rules before the run-wide coverage check. Verification and portfolio constraints can reduce this count; selected entries count each portfolio separately.')
else:
    st.caption('This older run did not save the pre-gate qualifying count. Zero verified contracts does not mean no opportunities were found.')
st.caption(f"Last saved: {stamp} • Last run started: {last.get('time', 'Unknown')} • Experiment started: {state['created']} • All times include their UTC offset.")
gate = last.get('entry_gate')
if gate and last.get('status') != 'Market closed':
    if not gate['allowed']:
        st.warning(gate['reason']+'. Existing positions are still managed. Missing quotes are not evidence that these stocks lack opportunities.')
    st.caption(f"Planned-scan coverage: ATM {gate.get('stage2_successful', 0)}/{gate.get('stage2_planned', 0)}; full scans {gate.get('stage3_complete', 0)}/{gate.get('stage3_planned', 0)}. This is coverage of the planned sample, not the entire index universe.")
elif not gate and last.get('stage2_checked', 0) > last.get('stage2_successful', 0):
    st.warning('This older run had failed ATM checks. The displayed ranking covers only the data that was available; the new recovery rules apply on the next updated run.')
if stamp and (datetime.now(timezone.utc)-datetime.fromisoformat(stamp)).total_seconds() > 5400:
    st.warning('Results are more than 90 minutes old. Markets may be closed; check run logs if a scheduled market-hours update is missing.')
st.caption('Full index universe → underlying diagnostics → ATM IV richness → full put scans + rotation → fresh rolling ranking → portfolio constraints → exact-contract verification and paper entry.')

overview, holdings, decisions, scanning, rules, health = st.tabs(['Results over time','Current holdings','Trades & decisions','Scan & selection audit','Strategy rules','Data & schedule'])
with overview:
    rows, points = [], []
    for key,m in state['models'].items():
        nav = m['history'][-1]['nav'] if m['history'] else CAPITAL
        reserve = sum(p['strike']*100 for p in m['positions'])
        closed = m['closed']
        rows.append({'Strategy':key+' · '+STRATEGIES[key]['name'], 'Portfolio value':nav,'Total P&L':nav-CAPITAL,'Return %':100*(nav/CAPITAL-1),'Available cash':m['cash']-reserve,'Open positions':len(m['positions']),'Closed trades':len(closed),'Realized P&L':sum(t['pnl'] for t in closed),'Win rate %':100*sum(t['pnl']>0 for t in closed)/len(closed) if closed else None,'Max drawdown %':100*m['max_drawdown'],'Fees':m['fees'],'Stale marks':m['history'][-1]['stale'] if m['history'] else 0})
        points.append(dict(Time=m.get('created', state['created']),Strategy=key,Value=CAPITAL))
        points.extend(dict(Time=p['time'],Strategy=key,Value=p['nav']) for p in m['history'])
    st.dataframe(pd.DataFrame(rows).set_index('Strategy'), use_container_width=True)
    chart = pd.DataFrame(points)
    chart['Time'] = pd.to_datetime(chart['Time'], utc=True)
    st.line_chart(chart.pivot_table(index='Time',columns='Strategy',values='Value',aggfunc='last'))
    st.caption('Portfolio value includes open option liabilities at the ask and fees. A stale mark carries the last known value; it is never changed to zero. Drawdown uses these observed values.')
    if not any(m['closed'] for m in state['models'].values()):
        st.info('No completed trades yet. These are forward results, not a backtest or a proven ranking.')
with holdings:
    key = st.selectbox('Portfolio',list(state['models']),format_func=lambda k:k+' · '+STRATEGIES[k]['name'])
    rows = []
    for p in state['models'][key]['positions']:
        rows.append({'Stock':p['ticker'],'Contract':p['contract'],'Expiry':p['expiry'],'Strike':p['strike'],'Entry credit':p['credit'],'Buyback mark':p['mark'],'Open P&L (before exit fee)':(p['credit']-p['mark'])*100-1,'Premium captured %':100*(1-p['mark']/p['credit']),'Entry Index':p['score'],'Entered':p['entered'],'Mark time':p['mark_time'],'Stale':p['mark_time']!=stamp})
    if rows:
        st.dataframe(pd.DataFrame(rows),use_container_width=True,hide_index=True)
    else:
        st.info('No open holdings. Cash stays undeployed until entry rules are met.')
with decisions:
    key2=st.selectbox('Strategy history',list(state['models']),format_func=lambda k:k+' · '+STRATEGIES[k]['name'])
    m=state['models'][key2]
    st.subheader('Closed trades')
    st.dataframe(pd.DataFrame(m['closed']),use_container_width=True,hide_index=True)
    st.subheader('Recent decisions')
    st.dataframe(pd.DataFrame(list(reversed(m['events']))[:500]),use_container_width=True,hide_index=True)
    st.download_button('Download all portfolios & history',json.dumps(state,indent=2),file_name='automated_paper_results.json',mime='application/json')
with rules:
    st.table(pd.DataFrame([{'Strategy':k,'Rule':v['name'],'Minimum entry Index':v['minimum'], 'Return / protection':f"{v['return_weight']}/{100-v['return_weight']}", 'Portfolio status':'Initialized' if k in state['models'] else 'Waiting for first saved run'} for k,v in STRATEGIES.items()]))
    st.write('Default entry scan: 21–60 DTE OTM puts, $3,000–$20,000 collateral. A–J retain 30% return / 70% protection. A40, A50 and A60 use the same rules as A with only entry weights changed. Scores are weighted harmonic means of return/day relative to 0.10%/day and cushion/expected move relative to 1.0. Expected move uses the larger of HV30 and ATM-IV moves. Each strategy ranks all cached contracts independently.')
    st.write('Baseline put execution excludes earnings in the holding period and unknown earnings timing. Earnings candidates remain visible with their scores. Entries require positive bids, spread at most 25% of ask, at least 100 open interest and a trade today. Saved run configuration shows the actual thresholds.')
    st.caption('A–J retain their historical portfolios. New weight portfolios start when migration runs; compare overlapping periods, since their start dates and capital paths differ. The entry pipeline change is recorded in migration history.')
    st.write('Entry sells at bid; early exit buys at ask; fee is $1 per contract per side. A stock closed this cycle cannot reopen until a later cycle. Loss limits are checked periodically, so losses can exceed the threshold between runs.')
    st.write('G closes a profitable put when captured premium is at least 35 percentage points above an entry-spot, entry-volatility Black–Scholes decay baseline. This is a model estimate, not guaranteed income.')
    st.write('At expiry, puts use cash-equivalent intrinsic settlement at the expiry session’s unadjusted stock close. This experiment does not model physical assignment, covered calls, dividends, interest, early assignment or exercise fees. It differs from a full wheel portfolio.')
    st.write('The separate legacy covered-call scanner allows earnings and labels that policy on its candidates. No automatic earnings exclusion is added to calls; covered-call execution is not part of these automated portfolios.')
with health:
    st.json(last)
    st.write('Universe source', state.get('universe',{}).get('source','Not loaded yet'))
    st.dataframe(pd.DataFrame(list(reversed(state.get('runs',[])))),use_container_width=True,hide_index=True)
    st.warning('Yahoo data may be delayed, unavailable or throttled. A trade today is a liquidity check, not a live bid/ask timestamp guarantee. Missing data pauses affected trades and appears in the log.')
    st.caption('Results and per-run audit files live on the paper-results branch and survive restarts. Only the scheduled runner changes portfolios. Loading this page never creates trades.')
    st.write('State migrations')
    st.json(state.get('migrations', []))

with scanning:
    with st.expander('Coverage age — persistent blind spots'):
        eligible = state.get('last_audit', {}).get('eligible_symbols', [])
        if not eligible:
            eligible = state.get('universe', {}).get('symbols', [])
        freshness = state.get('last_audit', {}).get('config', {}).get('freshness_minutes', 30)
        coverage = coverage_history(state, eligible, datetime.now(timezone.utc).timestamp(), freshness)
        st.write(f"{sum(r['overdue'] for r in coverage)} of {len(coverage)} tracked stocks have no complete scan within {freshness:g} minutes.")
        st.caption('Never-scanned and oldest complete scans receive rotation priority, subject to retry backoff. Scan age measures coverage; each contract separately retains its original quote age. Overnight ages naturally increase. Legacy scans without a saved completion time appear unknown.')
        for row in coverage:
            if row['last_complete_at'] is not None:
                row['last_complete_at'] = datetime.fromtimestamp(row['last_complete_at'], timezone.utc).isoformat()
        st.dataframe(pd.DataFrame(coverage), use_container_width=True, hide_index=True)
    with st.expander('Data recovery — failed and deferred stocks', expanded=bool(state.get('scan_retries'))):
        st.caption('Stocks stay queued until their stage succeeds. Each run reserves up to 40 ATM retry slots and 20 full-scan retry slots. Actual failures wait 30, 60, 120, then up to 240 minutes between attempts; unattempted work remains queued. Neither retries nor caching guarantee that Yahoo will supply the missing data.')
        health = state.get('provider_health', {})
        st.write('Last saved option-provider status', health.get('status', 'Not recorded yet'))
        if health.get('cooldown_until', 0):
            st.caption('Last cooldown ends: '+datetime.fromtimestamp(health['cooldown_until'], timezone.utc).isoformat())
        pending = []
        for r in state.get('scan_retries', {}).values():
            pending.append(dict(r, next_retry_at=datetime.fromtimestamp(r['next_retry_at'], timezone.utc).isoformat(),
                first_failed=datetime.fromtimestamp(r['first_failed'], timezone.utc).isoformat(),
                last_attempt=datetime.fromtimestamp(r['last_attempt'], timezone.utc).isoformat() if r['last_attempt'] else 'Not attempted'))
        st.dataframe(pd.DataFrame(pending), use_container_width=True, hide_index=True)
        st.caption(f"Option requests in saved run: {health.get('requests', 0)}; cache reuses: {health.get('cache_hits', 0)}. A cache reuse retains the original observation time.")
    st.subheader('Trace a run from universe to execution')
    audit = state.get('last_audit', {})
    saved_runs = [r for r in reversed(state.get('runs', [])) if r.get('audit_file')]
    choices = ['Latest saved run'] + [r['audit_file'] for r in saved_runs]
    choice = st.selectbox('Audit run', choices)
    audit_file = last.get('audit_file') if choice == choices[0] else choice
    if audit_file:
        try:
            audit = load_audit(audit_file)
        except Exception as exc:
            st.error(f'Cannot load selected audit: {exc}')
            audit = {}
    if not audit:
        st.info('Full scan audits appear after the updated runner saves a market-hours run.')
    else:
        summary = audit.get('summary', last)
        metrics = st.columns(6)
        for col, label, field in zip(metrics,
                ['Total universe', 'Stage 1 eligible', 'ATM names checked', 'Full scans completed', 'Fresh eligible tickers at run', 'Verified contracts'],
                ['checked', 'eligible_underlyings', 'stage2_checked', 'option_scanned', 'rolling_fresh_coverage', 'candidates']):
            col.metric(label, summary.get(field, 0))
        st.caption('Fresh coverage counts tickers with at least one eligible cached contract. It does not mean every index stock has a current option score. Partial, failed and deferred scans are shown below.')
        st.write('Audit time', audit.get('time'))
        st.download_button('Download this complete run audit', json.dumps(audit, indent=2),
            file_name='scan_audit.json', mime='application/json')
        with st.expander('Universe and scan configuration'):
            st.json(audit.get('config', {}))
            st.dataframe(pd.DataFrame({'Ticker': audit.get('universe', state.get('universe', {})).get('symbols', [])}), hide_index=True)
        with st.expander('Stage 1 — every underlying, metrics and rejection reasons'):
            st.dataframe(pd.DataFrame(audit.get('stage1', [])), use_container_width=True, hide_index=True)
        with st.expander('Stage 2 — ATM IV, IV richness, liquidity and earnings'):
            st.caption('Prescreen score = ATM IV / HV30 × (1 − ATM spread/ask). Earnings are labeled, never removed here. Missing data stays unavailable. The underlying proxy orders the first-stage budget only.')
            st.dataframe(pd.DataFrame(audit.get('stage2', [])), use_container_width=True, hide_index=True)
            st.write('Planned but deferred', audit.get('stage2_deferred', []))
        with st.expander('Stage 3 — full scans, rotation, and all observed contracts'):
            st.dataframe(pd.DataFrame(audit.get('stage3', [])), use_container_width=True, hide_index=True)
            st.caption('empty_confirmed means Yahoo explicitly returned an empty put list twice with calls present. It is a provider-reported empty result, not independent proof that no puts exist. Missing fields, both sides empty and failed requests remain incomplete.')
            st.dataframe(pd.DataFrame([e for r in audit.get('stage3', []) for e in r.get('expiry_audit', [])]), use_container_width=True, hide_index=True)
            st.write('Planned but deferred', audit.get('stage3_deferred', []))
            st.dataframe(pd.json_normalize(audit.get('contract_audit', [])), use_container_width=True, hide_index=True)
        st.subheader('Full rolling candidate ranking at this run')
        weight = st.selectbox('Return / protection weighting', [30, 40, 50, 60], format_func=lambda w:f'{w}% / {100-w}%')
        ranking = []
        for row in audit.get('rolling_ranking', []):
            r = dict(row)
            r['score'] = r.get('scores', {}).get(str(weight), r['score'])
            r['rejections'] = '; '.join(r.get('rejections', []))
            ranking.append(r)
        ranking.sort(key=lambda r: (-r['score'], r['contract']))
        st.dataframe(pd.json_normalize([dict(r, rank=i) for i, r in enumerate(ranking, 1)]), use_container_width=True, hide_index=True)
        st.subheader('Portfolio constraints and selected contracts')
        strategy = st.selectbox('Entry decision strategy', list(state['models']), format_func=lambda k:k+' · '+STRATEGIES[k]['name'])
        decisions_for_model = [r for r in audit.get('portfolio_constraints', []) if r['strategy'] == strategy]
        selected = [r for r in decisions_for_model if r['decision'] == 'selected']
        if selected:
            st.dataframe(pd.DataFrame(selected), use_container_width=True, hide_index=True)
        else:
            st.info('No contract selected for this strategy in this run.')
        st.dataframe(pd.DataFrame(decisions_for_model), use_container_width=True, hide_index=True)
    st.subheader('Rolling best contract per ticker — age now')
    cfg = ScanConfig(**state.get('last_audit', {}).get('config', {}))
    live_rows = rolling_ranking(state.get('option_cache', {}), datetime.now(timezone.utc).timestamp(),
        cfg, set(state.get('last_audit', {}).get('eligible_symbols', [])))
    table = []
    for ticker, entry in state.get('option_cache', {}).items():
        for weight, best in entry.get('best_by_weight', {}).items():
            matching = next((c for c in live_rows if c['contract'] == best['contract']), {})
            table.append(dict(ticker=ticker, return_weight=weight, contract=best['contract'], score=best['score'],
                return_score=best['return_score'], protection_score=best['protection_score'],
                scanned_at=datetime.fromtimestamp(best['fetched'], timezone.utc).isoformat(),
                age_minutes=matching.get('age_minutes'), status=entry['status'],
                rejections='; '.join(matching.get('rejections', ['unavailable']))))
    st.dataframe(pd.DataFrame(table), use_container_width=True, hide_index=True)
    st.subheader('Missed-opportunity audit')
    st.caption('Rotation discoveries in the top ten scanned eligible stocks, or at least the configured number of Index points above the best prescreened stock. This measures the observed sample, not unseen market opportunities. Complete history is retained in per-run audit files.')
    st.dataframe(pd.DataFrame(state.get('missed_opportunity_audit', [])), use_container_width=True, hide_index=True)
