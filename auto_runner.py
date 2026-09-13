"""Scheduled paper experiment; persistent state is owned only by this runner."""
from __future__ import annotations
import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd
import pandas_market_calendars as mcal
import requests
import yfinance as yf
from engine import scan_put_ticker
from universe import load_sp500_constituents, prescreen_underlyings
from paper_core import fresh_state, run_cycle, valid_number, VERSION

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

def exact_quote(p, today):
    frame = yf.Ticker(p['ticker']).option_chain(p['expiry']).puts
    match = frame[frame['contractSymbol'] == p['contract']]
    if len(match) != 1:
        raise ValueError('Exact contract absent')
    row = match.iloc[0]
    bid, ask = float(row['bid']), float(row['ask'])
    traded = pd.Timestamp(row['lastTradeDate']).tz_convert(NY)
    if not (ask > 0 and bid >= 0 and ask >= bid and traded.date() == today):
        raise ValueError('Invalid spread or no trade today; keep prior valuation')
    return dict(ask=ask, bid=bid)

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

def run(path):
    now = datetime.now(NY)
    state = json.loads(path.read_text()) if path.exists() else fresh_state(now.isoformat())
    if state.get('version') != VERSION or set(state.get('models', {})) != set(fresh_state('')['models']):
        raise RuntimeError('Unexpected state schema; refusing to reset portfolios')
    started = time.monotonic()
    warnings, candidates, quotes, settlements = [], [], {}, {}
    sched = session(now.date())
    is_open = sched is not None and sched['market_open'] <= now <= sched['market_close']
    report = dict(time=now.isoformat(), status='Market closed', checked=0, selected=0, candidates=0)
    slot = now.strftime('%Y-%m-%dT%H:')+('00' if now.minute < 30 else '30')
    if state.get('last_slot') == slot:
        return
    if is_open:
        try:
            universe = load_universe(state, now)
            state['universe'] = universe
            pre, warns = prescreen_underlyings(tuple(universe['symbols']), 3000., 20000., 50., 1_000_000.)
            warnings.extend(warns)
            report['checked'] = len(universe['symbols'])
            report['eligible_underlyings'] = len(pre)
            # Top 20 plus rotating 20: bounded public-data load, broad coverage over time.
            ranked = pre['Ticker'].tolist() if not pre.empty else []
            rest = sorted(ranked[20:])
            cursor = int(state.get('scan_cursor', 0)) % max(len(rest), 1)
            rotating = (rest[cursor:]+rest[:cursor])[:20]
            selected = ranked[:20]+rotating
            state['scan_cursor'] = cursor+20
            report['selected'] = len(selected)
            report['option_scanned'] = 0
            for symbol in selected:
                if time.monotonic()-started > 780:
                    warnings.append('Scan time budget reached; remaining names deferred')
                    break
                try:
                    frame, warns = scan_put_ticker(symbol, 21, 60, 3000., 20000., 0., 30, True)
                    warnings.extend(warns)
                    report['option_scanned'] += 1
                    for _, r in frame.iterrows():
                        bid, ask = float(r['Bid']), float(r['Ask'])
                        if not (bid > 0 and ask >= bid and (ask-bid)/ask <= .25):
                            continue
                        # Earnings unknown is not treated as earnings-free.
                        if r['Earnings in Period'] != 'No' or not bool(r.get('Earnings Known', False)):
                            continue
                        oi, iv = float(r['Opportunity Index']), float(r['Contract IV'])
                        if not valid_number(oi) or not valid_number(iv) or iv <= 0:
                            continue
                        if float(r.get('Open Interest', 0)) < 100:
                            continue
                        traded = pd.Timestamp(r.get('Last Trade Date'))
                        if pd.isna(traded) or traded.tz_convert(NY).date() != now.date():
                            continue
                        candidates.append(dict(ticker=symbol, contract=str(r['Contract']), expiry=str(r['Expiry']), strike=float(r['Strike']), dte=int(r['DTE']), bid=bid, ask=ask, score=oi, iv=iv, spot=float(r['Stock Price']), fetched=time.time()))
                except Exception as exc:
                    warnings.append(f'{symbol}: {exc}')
            report['status'] = 'Completed' if not warnings else 'Completed with data warnings'
        except Exception as exc:
            warnings.append(f'New entries paused: {exc}')
            report['status'] = 'Entry scan unavailable'
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
    finished = datetime.now(NY)
    still_open = sched is not None and sched['market_open'] <= finished < sched['market_close']-timedelta(minutes=15)
    candidates = [c for c in candidates if time.time()-c['fetched'] <= 600]
    # Entries are re-quoted by exact contract, preventing stale scan fills.
    verified = []
    for c in sorted(candidates, key=lambda x:-x['score']):
        if time.monotonic()-started > 1000 or len(verified) >= 20 or not still_open:
            break
        try:
            q = exact_quote(c, finished.date())
            if q['bid'] < c['bid'] or q['ask'] > c['ask']:
                continue  # Re-score on next cycle instead of using an obsolete entry score.
            verified.append(c)
        except Exception as exc:
            warnings.append(f"Entry {c['contract']}: {exc}")
    finished = datetime.now(NY)
    still_open = sched is not None and sched['market_open'] <= finished < sched['market_close']-timedelta(minutes=15)
    report['candidates'] = len(verified)
    report['warnings'] = warnings[-100:]
    report['duration_seconds'] = round(time.monotonic()-started)
    state = run_cycle(state, verified, quotes if is_open and finished <= sched['market_close'] else {}, settlements, finished.isoformat(), slot, still_open)
    state['last_run'] = report
    state['runs'] = (state.get('runs', [])+[report])[-200:]
    state['candidates'] = verified
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, indent=2, allow_nan=False), encoding='utf-8')
    os.replace(tmp, path)
    print(json.dumps(report))

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', type=Path, default=Path('state.json'))
    run(parser.parse_args().state)
