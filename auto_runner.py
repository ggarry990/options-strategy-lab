"""Scheduled paper experiment; persistent state is owned only by this runner."""
from __future__ import annotations
import argparse
import json
import gzip
import os
import time
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo
from dataclasses import asdict
import pandas as pd
import pandas_market_calendars as mcal
import requests
import yfinance as yf
from universe import load_sp500_constituents
from paper_core import fresh_state, run_cycle, valid_number, migrate_state
from pipeline import ScanConfig, scan_pipeline, rolling_ranking, best_by_weight, clean
from scoring import score_candidate
from scan_schedule import slot_key, already_processed
from yahoo_options import ACTIVE, YahooOptions
from scan_recovery import record_retry

NY = ZoneInfo('America/New_York')
CAL = mcal.get_calendar('NYSE')

def session(day):
    frame = CAL.schedule(start_date=day, end_date=day)
    return None if frame.empty else frame.iloc[0]

def load_universe(state, now):
    cache = state.get('universe', {})
    if cache.get('date') == now.date().isoformat():
        return cache
    sp, source, warning = load_sp500_constituents()
    if warning:
        raise RuntimeError(warning)  # Never substitute a non-index fallback.
    url = 'https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies'
    response = requests.get(url, headers={'User-Agent':'Mozilla/5.0 OptionsStrategyLab/1.0'}, timeout=25)
    response.raise_for_status()
    tables = pd.read_html(StringIO(response.text))
    nasdaq = next(t for t in tables if 'Ticker' in t.columns and len(t) >= 95 and len(t) <= 110)
    symbols = sorted(set(sp['Symbol']) | {str(s).replace('.', '-') for s in nasdaq['Ticker']})
    return dict(date=now.date().isoformat(), symbols=symbols, sp500=len(sp), nasdaq100=len(nasdaq), source=f'S&P: {source}; Nasdaq-100: Wikipedia constituents')

def exact_quote(p, today, chain_cache=None):
    # Reuse a newly fetched expiry across exact contracts in this verification pass.
    # Existing-position calls keep their original behavior (no shared cache).
    key = (p['ticker'], p['expiry'])
    cached = chain_cache.get(key) if chain_cache is not None else None
    if cached is not None and 0 <= time.time()-cached[0] <= 45:
        observed, frame = cached
    else:
        if chain_cache is not None and ACTIVE.get():
            chain = ACTIVE.get().chain(p['ticker'], p['expiry'], max_age=45)
            frame, observed = chain.puts, chain.observed_at
        else:
            # Position management remains independent of the entry-scan cooldown.
            frame = yf.Ticker(p['ticker']).option_chain(p['expiry']).puts
            observed = time.time()
        if chain_cache is not None:
            chain_cache[key] = (observed, frame)
    match = frame[frame['contractSymbol'] == p['contract']]
    if len(match) != 1:
        raise ValueError('Exact contract absent')
    row = match.iloc[0]
    bid, ask = float(row['bid']), float(row['ask'])
    traded = pd.Timestamp(row['lastTradeDate']).tz_convert(NY)
    if not (ask > 0 and bid >= 0 and ask >= bid and traded.date() == today):
        raise ValueError('Invalid spread or no trade today; keep prior valuation')
    return dict(ask=ask, bid=bid, observed_at=observed)

def expiry_close(p, now):
    # The previous session handles rare expirations falling on a market holiday.
    expiry = pd.Timestamp(p['expiry']).date()
    sched = CAL.schedule(start_date=expiry-timedelta(days=7), end_date=expiry)
    row = sched.iloc[-1]
    if now.astimezone(timezone.utc) < row['market_close'].to_pydatetime()+timedelta(minutes=20):
        return None
    day = sched.index[-1].date()
    hist = yf.Ticker(p['ticker']).history(start=day.isoformat(), end=(day+timedelta(days=1)).isoformat(), auto_adjust=False)
    if hist.empty or pd.Timestamp(hist.index[-1]).date() != day:
        raise ValueError('Expiry-session close unavailable')
    value = float(hist['Close'].iloc[-1])
    if not valid_number(value) or value <= 0:
        raise ValueError('Invalid expiry close')
    return value

def run(path, config=None):
    config = config or ScanConfig()
    now = datetime.now(NY)
    state = migrate_state(json.loads(path.read_text(encoding='utf-8')), now.isoformat()) if path.exists() else fresh_state(now.isoformat())
    with YahooOptions(state, spacing=config.option_request_spacing) as reader:
        return _run_loaded(path, config, now, state, reader)


def _run_loaded(path, config, now, state, reader):
    started = time.monotonic()
    warnings, quotes, settlements = [], {}, {}
    sched = session(now.date())
    is_open = sched is not None and sched['market_open'] <= now <= sched['market_close']
    report = dict(time=now.isoformat(), status='Market closed', checked=0, selected=0, candidates=0)
    slot = slot_key(now)
    if already_processed(state.get('last_slot'), now):
        return
    audit = dict(config=asdict(config), stage1=[], stage2=[], stage3=[], rolling_ranking=[], eligible_symbols=[])
    if is_open:
        try:
            universe = load_universe(state, now)
            state['universe'] = universe
            audit = scan_pipeline(state, universe, now, config, started)
            warnings.extend(audit['warnings'])
            report['status'] = 'Completed' if not warnings else 'Completed with data warnings'
        except Exception as exc:
            warnings.append(f'New entries paused: {exc}')
            report['status'] = 'Entry scan unavailable'
    # Preserve exit/valuation behavior with the existing exact-quote and settlement rules.
    positions = {p['contract']:p for m in state['models'].values() for p in m['positions']}
    for contract, p in positions.items():
        try:
            if p['expiry'] <= now.date().isoformat():
                px = expiry_close(p, datetime.now(NY))
                if px is not None:
                    settlements[contract] = px
                    continue
            if is_open:
                quotes[contract] = exact_quote(p, now.date())
        except Exception as exc:
            warnings.append(f'{contract}: {exc}')
    # Recompute age after scanning and position checks; a failed universe gates entries.
    candidates = rolling_ranking(state.get('option_cache', {}), time.time(), config, set(audit['eligible_symbols']))
    gate = audit.get('entry_gate', dict(allowed=False, reason='Market closed' if not is_open else 'New entries paused: scan coverage unavailable'))
    if not gate['allowed']:
        for c in candidates:
            c['rejections'].append(gate['reason'])
        if is_open:
            warnings.append(gate['reason'])
            report['status'] = 'New entries paused: incomplete scan coverage'
    # Interleave rankings so verification budgets do not privilege the 30/70 model.
    queues = [sorted((c for c in candidates if not c['rejections']),
                     key=lambda c: (-score_candidate(c, w), c['contract'])) for w in (30, 40, 50, 60)]
    order, seen = [], set()
    for rank in range(max((len(q) for q in queues), default=0)):
        for q in queues:
            if rank < len(q) and q[rank]['contract'] not in seen:
                order.append(q[rank])
                seen.add(q[rank]['contract'])
    verification_chains = {}
    quote_retry_tickers = set()
    for c in order:
        finished = datetime.now(NY)
        still_open = sched is not None and sched['market_open'] <= finished < sched['market_close']-timedelta(minutes=15)
        if not still_open:
            c['rejections'].append('entry window closed')
            continue
        if time.monotonic()-started >= config.entry_budget_seconds:
            c['rejections'].append('verification deferred: time budget')
            continue
        if not 0 <= time.time()-c['fetched'] <= config.freshness_minutes*60:
            c['rejections'].append('stale data: cache freshness')
            continue
        try:
            q = exact_quote(c, finished.date(), verification_chains)
            if q['bid'] < c['bid'] or q['ask'] > c['ask']:
                c['rejections'].append('quote changed adversely: rescan required')
            elif q['bid'] <= 0 or (q['ask']-q['bid'])/q['ask'] > config.max_spread:
                c['rejections'].append('spread on entry verification')
            else:
                c['verified_at'] = q.get('observed_at', time.time())
        except Exception as exc:
            c['rejections'].append('exact quote unavailable')
            warnings.append(f"Entry {c['contract']}: {exc}")
            if c['ticker'] not in quote_retry_tickers:
                record_retry(state, c['ticker'], 'stage3', f'Entry quote unavailable: {exc}', time.time())
                quote_retry_tickers.add(c['ticker'])
    finished = datetime.now(NY)
    still_open = sched is not None and sched['market_open'] <= finished < sched['market_close']-timedelta(minutes=15)
    for c in candidates:
        if reader.paused:
            c['rejections'].append('Yahoo access cooldown: new entries paused')
        c['age_minutes'] = round((time.time()-c['fetched'])/60, 2)
        if not 0 <= time.time()-c['fetched'] <= config.freshness_minutes*60:
            c['rejections'].append('stale data: cache freshness')
        if not c['rejections'] and 'verified_at' not in c:
            c['rejections'].append('exact quote unverified')
        c['rejections'] = list(dict.fromkeys(c['rejections']))
    fresh_rows = rolling_ranking(state.get('option_cache', {}), time.time(), config, set(audit['eligible_symbols']))
    if any('verification deferred: time budget' in c['rejections'] for c in candidates):
        warnings.append('Entry verification budget reached; unverified contracts were not executed')
    report.update(checked=len(state.get('universe', {}).get('symbols', [])),
        eligible_underlyings=len(audit['eligible_symbols']), stage2_checked=len(audit['stage2']),
        stage2_successful=sum(r.get('status') == 'checked' for r in audit['stage2']),
        selected=len(audit['stage3']), option_scanned=sum(r['status'] == 'complete' for r in audit['stage3']),
        rolling_fresh_coverage=len({c['ticker'] for c in fresh_rows if not c['rejections']}),
        candidates=sum(not c['rejections'] for c in candidates), warnings=warnings,
        duration_seconds=round(time.monotonic()-started))
    state['provider_health'] = reader.health()
    audit['provider_health'] = reader.health()
    audit['entry_gate'] = gate
    audit['retry_queue'] = list(state.get('scan_retries', {}).values())
    report['entry_gate'] = dict(gate, allowed=gate['allowed'] and not reader.paused)
    report['pending_retries'] = len(state.get('scan_retries', {}))
    if reader.paused:
        report['status'] = 'New entries paused: Yahoo access cooldown'
        report['entry_gate']['reason'] = 'Yahoo access cooldown: new entries paused'
    audit['entry_gate'] = report['entry_gate']
    if warnings and report['status'] == 'Completed':
        report['status'] = 'Completed with data warnings'
    state = run_cycle(state, candidates, quotes if is_open and finished <= sched['market_close'] else {},
        settlements, finished.isoformat(), slot, still_open and report['entry_gate']['allowed'],
        report['entry_gate']['reason'] if still_open and not report['entry_gate']['allowed'] else None)
    audit.update(time=now.isoformat(), summary=report, rolling_ranking=candidates,
        portfolio_constraints=state['entry_audit'],
        execution=[r for r in state['entry_audit'] if r['decision'] == 'selected'],
        path=['universe', 'stage1', 'stage2', 'stage3', 'rolling_ranking', 'portfolio_constraints', 'execution'])
    # Each complete audit is an immutable file. State retains the latest metadata.
    audit_name = 'audits/'+now.strftime('%Y%m%dT%H%M%S%z')+'.json.gz'
    report['audit_file'] = audit_name
    state['last_run'] = report
    state['runs'] = (state.get('runs', [])+[report])[-200:]
    # Keep large contract/decision tables in the compressed immutable audit, not
    # duplicated throughout state.json. The dashboard loads them on demand.
    state['last_audit'] = {k:v for k,v in audit.items() if k not in
        ('contract_audit', 'rolling_ranking', 'portfolio_constraints')}
    state.pop('entry_audit', None)
    winners = {}
    for ticker in {c['ticker'] for c in candidates}:
        for c in best_by_weight([c for c in candidates if c['ticker'] == ticker]).values():
            winners[c['contract']] = c
    state['candidates'] = list(winners.values())
    discoveries = [dict(r, time=now.isoformat(), audit_file=audit_name) for r in audit.get('missed_opportunities', [])]
    state['missed_opportunity_audit'] = (state.get('missed_opportunity_audit', [])+discoveries)[-2000:]
    state = clean(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    audit_path = path.parent/audit_name
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_bytes(gzip.compress(json.dumps(clean(audit), allow_nan=False).encode('utf-8'), mtime=0))
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, indent=2, allow_nan=False), encoding='utf-8')
    os.replace(tmp, path)
    print(json.dumps(report))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', type=Path, default=Path('state.json'))
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('scan_config.json'))
    args = parser.parse_args()
    run(args.state, ScanConfig(**json.loads(args.config.read_text(encoding='utf-8'))))
