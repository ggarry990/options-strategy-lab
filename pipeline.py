"""Broad scan orchestration and durable, inspectable candidate selection."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import math
import time

import pandas as pd
import yfinance as yf

from engine import (get_option_expirations, get_option_chain, get_atm_iv,
                    get_earnings_dates, next_earnings_date, scan_put_ticker)
from scoring import score_candidate
from universe import prescreen_underlyings

WEIGHTS = (30, 40, 50, 60)


@dataclass(frozen=True)
class ScanConfig:
    stage2_limit: int = 180
    stage2_rotation: int = 40
    stage3_limit: int = 100
    audit_rotation: int = 20
    freshness_minutes: float = 30
    min_dte: int = 21
    max_dte: int = 60
    min_cash: float = 3000
    max_cash: float = 20000
    min_avg_volume: float = 1_000_000
    max_spread: float = .25
    min_open_interest: int = 100
    scan_budget_seconds: int = 600
    entry_budget_seconds: int = 780
    material_improvement: float = 5  # Absolute Opportunity Index points.

    def __post_init__(self):
        if any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in asdict(self).values()):
            raise ValueError('Scan settings must be finite numbers')
        for name in ('stage2_limit', 'stage2_rotation', 'stage3_limit', 'audit_rotation', 'min_dte', 'max_dte'):
            if not isinstance(getattr(self, name), int):
                raise ValueError(f'{name} must be an integer')
        if not 150 <= self.stage2_limit <= 200 or not 75 <= self.stage3_limit <= 100:
            raise ValueError('Stage 2 must be 150–200; Stage 3 must be 75–100')
        if not 0 < self.stage2_rotation < self.stage2_limit or self.audit_rotation < 1:
            raise ValueError('Both stages require positive rotation capacity')
        if not 0 < self.freshness_minutes or not 0 < self.min_dte <= self.max_dte:
            raise ValueError('Invalid freshness or DTE range')
        if not 0 < self.min_cash <= self.max_cash or self.min_avg_volume < 0:
            raise ValueError('Invalid collateral or volume range')
        if not 0 < self.max_spread <= 1 or self.min_open_interest < 0:
            raise ValueError('Invalid option liquidity thresholds')
        if not 0 < self.scan_budget_seconds < self.entry_budget_seconds <= 1300:
            raise ValueError('Budgets must reserve time for entry checks and saving')
        if self.material_improvement < 0:
            raise ValueError('Material improvement cannot be negative')


def clean(value):
    """Strict JSON output: unavailable data is null, never fabricated as zero."""
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if hasattr(value, 'item'):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    return value


def rotate(symbols, count, cursor):
    names = sorted(set(symbols))
    if not names:
        return [], 0
    start = cursor % len(names)
    selected = (names[start:] + names[:start])[:count]
    return selected, (start + len(selected)) % len(names)


def atm_snapshot(row, today):
    symbol, spot, hv = row['Ticker'], row['Stock Price'], row['HV30']
    ticker = yf.Ticker(symbol)
    expirations, error = get_option_expirations(ticker, symbol)
    choices = [(abs((pd.Timestamp(e).date()-today).days-37.5), e) for e in expirations
               if 30 <= (pd.Timestamp(e).date()-today).days <= 45]
    if not choices:
        raise ValueError(error or 'No representative expiry at 30–45 DTE')
    expiry = min(choices)[1]
    chain, error = get_option_chain(ticker, symbol, expiry)
    if chain is None:
        raise ValueError(error)
    iv = float(get_atm_iv(chain, spot))
    spreads, interests, volumes = [], [], []
    for side in (chain.calls, chain.puts):
        if side.empty:
            continue
        nearest = side.assign(distance=(pd.to_numeric(side['strike'], errors='coerce')-spot).abs()).nsmallest(1, 'distance')
        for _, option in nearest.iterrows():
            bid, ask = float(option.get('bid', float('nan'))), float(option.get('ask', float('nan')))
            if ask > 0 and 0 <= bid <= ask:
                spreads.append((ask-bid)/ask)
            interests.append(float(option.get('openInterest', float('nan'))))
            volumes.append(float(option.get('volume', float('nan'))))
    spread = max(spreads) if spreads else None
    richness = iv/hv if math.isfinite(iv) and iv > 0 and math.isfinite(hv) and hv > 0 else None
    # Richness, discounted by quoted ATM spread; raw IV never determines rank.
    rank_score = richness * (1-spread) if richness is not None and spread is not None else None
    earnings = get_earnings_dates(ticker)
    return clean(dict(ticker=symbol, expiry=expiry, atm_iv=iv, hv30=hv, iv_richness=richness,
        atm_spread=spread, atm_open_interest=sum(x for x in interests if math.isfinite(x)) if any(math.isfinite(x) for x in interests) else None,
        atm_volume=sum(x for x in volumes if math.isfinite(x)) if any(math.isfinite(x) for x in volumes) else None,
        next_earnings=next_earnings_date(earnings, today),
        earnings_in_period=any(today <= d <= pd.Timestamp(expiry).date() for d in earnings),
        prescreen_score=rank_score, status='checked' if rank_score is not None else 'ATM data incomplete',
        observed_at=datetime.now(timezone.utc).isoformat()))


def candidate_from_row(row, today, config):
    r = row
    reasons = []
    bid, ask = float(r['Bid']), float(r['Ask'])
    if not (math.isfinite(bid) and math.isfinite(ask) and bid > 0 and ask >= bid and (ask-bid)/ask <= config.max_spread):
        reasons.append('spread or invalid bid/ask')
    if r['Earnings in Period'] != 'No':
        reasons.append('earnings in period')
    if not bool(r.get('Earnings Known', False)):
        reasons.append('earnings unknown')
    iv = float(r['Contract IV'])
    if not math.isfinite(iv) or iv <= 0:
        reasons.append('contract IV unavailable')
    oi = float(r.get('Open Interest', float('nan')))
    if not math.isfinite(oi) or oi < config.min_open_interest:
        reasons.append('open interest')
    try:
        raw_trade = r.get('Last Trade Date')
        traded = pd.to_datetime(raw_trade, unit='s', utc=True) if isinstance(raw_trade, (int, float)) else pd.to_datetime(raw_trade, utc=True)
        if pd.isna(traded) or traded.tz_convert('America/New_York').date() != today:
            reasons.append('stale trade data')
    except (ValueError, TypeError, OverflowError):
        reasons.append('stale trade data')
    result = dict(ticker=str(r['Ticker']), contract=str(r['Contract']), expiry=str(r['Expiry']),
        strike=float(r['Strike']), dte=int(r['DTE']), bid=bid, ask=ask, iv=iv,
        spot=float(r['Stock Price']), fetched=float(r['Observed At']),
        return_score=float(r['Return / Day'])/.001*100,
        protection_score=float(r['Protection Ratio'])*100,
        expected_move=float(r['Expected Move']), cushion=float(r['Cushion']),
        atm_iv=float(r['ATM IV']), hv30=float(r['HV30']), open_interest=oi,
        earnings=r['Earnings in Period'], earnings_known=bool(r.get('Earnings Known', False)),
        last_trade=str(r.get('Last Trade Date')), rejections=reasons)
    result['score'] = score_candidate(result)
    result['scores'] = {str(w): score_candidate(result, w) for w in WEIGHTS}
    if not math.isfinite(result['return_score']) or not math.isfinite(result['protection_score']):
        reasons.append('score components unavailable')
    return clean(result)


def rolling_ranking(cache, now_epoch, config, eligible_symbols):
    rows = []
    today = datetime.fromtimestamp(now_epoch, ZoneInfo('America/New_York')).date()
    for ticker, entry in cache.items():
        for saved in entry.get('contracts', []):
            c = dict(saved, rejections=list(saved.get('rejections', [])))
            age = (now_epoch-c['fetched'])/60
            c['age_minutes'] = round(age, 2)
            c['dte'] = (datetime.fromisoformat(c['expiry']).date()-today).days
            if not 0 <= age <= config.freshness_minutes:
                c['rejections'].append('stale data: cache freshness')
            if entry.get('status') != 'complete':
                c['rejections'].append('stale data: latest scan unavailable or incomplete')
            if ticker not in eligible_symbols:
                c['rejections'].append('not Stage 1 eligible in current universe')
            if not config.min_dte <= c['dte'] <= config.max_dte:
                c['rejections'].append('DTE outside current configured range')
            if not config.min_cash <= c['strike']*100 <= config.max_cash:
                c['rejections'].append('collateral outside current configured range')
            if c.get('open_interest') is None or c['open_interest'] < config.min_open_interest:
                c['rejections'].append('open interest')
            if c.get('bid') is None or c.get('ask') is None or not (c['ask'] >= c['bid'] > 0 and (c['ask']-c['bid'])/c['ask'] <= config.max_spread):
                c['rejections'].append('spread or invalid bid/ask')
            if datetime.fromtimestamp(c['fetched'], ZoneInfo('America/New_York')).date() != today:
                c['rejections'].append('stale data: prior session')
            c['rejections'] = list(dict.fromkeys(c['rejections']))
            rows.append(c)
    return sorted(rows, key=lambda c: (-c['score'], c['contract']))


def best_by_weight(contracts):
    eligible = [c for c in contracts if not c.get('rejections')]
    best = {}
    for weight in WEIGHTS:
        if eligible:
            winner = min(eligible, key=lambda c: (-score_candidate(c, weight), c['contract']))
            best[str(weight)] = dict(winner, score=score_candidate(winner, weight))
    return best


def missed_opportunities(rows, primary, rotating, material=5):
    discoveries = []
    for weight in WEIGHTS:
        # Compare best contract per stock, so ten strikes don't occupy the top ten.
        best = {}
        for c in rows:
            if c.get('rejections') or c['ticker'] not in set(primary) | set(rotating):
                continue
            if c['ticker'] not in best or score_candidate(c, weight) > score_candidate(best[c['ticker']], weight):
                best[c['ticker']] = c
        ordered = sorted(best.values(), key=lambda c: (-score_candidate(c, weight), c['contract']))
        primary_scores = [score_candidate(c, weight) for c in ordered if c['ticker'] in primary]
        top = max(primary_scores) if primary_scores else None
        for rank, c in enumerate(ordered, 1):
            score = score_candidate(c, weight)
            if c['ticker'] in rotating and (rank <= 10 or top is not None and score >= top+material):
                discoveries.append(dict(ticker=c['ticker'], contract=c['contract'], weight=weight,
                    score=score, rank=rank, best_prescreened_score=top,
                    improvement=score-top if top is not None else None,
                    reason='top 10 among scanned eligible stocks' if rank <= 10 else 'material improvement'))
    return discoveries


def scan_pipeline(state, universe, now, config, started):
    deadline = started+config.scan_budget_seconds
    report = dict(config=asdict(config), universe=universe, stage1=[], stage2=[], stage3=[],
                  contract_audit=[], warnings=[], primary=[], rotating=[])
    pre, warnings = prescreen_underlyings(tuple(universe['symbols']), config.min_cash,
        config.max_cash, min_avg_volume=config.min_avg_volume, include_rejected=True)
    report['warnings'].extend(warnings)
    report['stage1'] = clean(pre.to_dict('records'))
    # A daily history without today's bar must not silently pass the price gate.
    for r in report['stage1']:
        if r.get('Eligible') and pd.Timestamp(r['Price Date']).date() != now.date():
            r['Eligible'] = False
            r['Rejection Reasons'] = 'stale underlying price'
    eligible = [r['Ticker'] for r in report['stage1'] if r.get('Eligible')]
    lookup = {r['Ticker']: r for r in report['stage1']}
    top_count = config.stage2_limit-config.stage2_rotation
    extra, cursor = rotate(eligible[top_count:], config.stage2_rotation, state.get('atm_cursor', 0))
    core = eligible[:top_count]
    stage2_names = []
    for i in range(max(len(core), len(extra))):
        if i < len(core):
            stage2_names.append(core[i])
        if i < len(extra):
            stage2_names.append(extra[i])
    # Advance only by the rotation names actually attempted, including failures.
    attempted_extra = 0
    for symbol in stage2_names:
        if time.monotonic() >= started+config.scan_budget_seconds*.45:
            break
        try:
            report['stage2'].append(atm_snapshot(lookup[symbol], now.date()))
        except Exception as exc:
            report['stage2'].append(dict(ticker=symbol, status='unavailable', error=str(exc)))
            report['warnings'].append(f'{symbol} ATM: {exc}')
        attempted_extra += symbol in extra
    _, state['atm_cursor'] = rotate(eligible[top_count:], attempted_extra, state.get('atm_cursor', 0))
    ranked = sorted((r for r in report['stage2'] if r.get('prescreen_score') is not None),
                    key=lambda r: (-r['prescreen_score'], r['ticker']))
    primary = [r['ticker'] for r in ranked[:config.stage3_limit]]
    rotating, _ = rotate(set(eligible)-set(primary), config.audit_rotation, state.get('scan_cursor', 0))
    report.update(primary=primary, rotating=rotating,
        stage2_planned=stage2_names, stage2_deferred=stage2_names[len(report['stage2']):])
    cache = state.setdefault('option_cache', {})
    completed_rotating = 0
    # Interleave the audit cohort so a deadline cannot starve it every run.
    order = []
    for i in range(max(len(primary), len(rotating))):
        if i < len(primary):
            order.append(primary[i])
        if i < len(rotating):
            order.append(rotating[i])
    scanned_rows = []
    for symbol in order:
        if time.monotonic() >= deadline:
            break
        attempted = time.time()
        entry = cache.setdefault(symbol, dict(contracts=[]))
        try:
            frame, warns = scan_put_ticker(symbol, config.min_dte, config.max_dte,
                config.min_cash, config.max_cash, 0., 30, True,
                audit_rows=report['contract_audit'], asof=now.date(), deadline=deadline)
            report['warnings'].extend(warns)
            contracts = [candidate_from_row(r, now.date(), config) for _, r in frame.iterrows()]
            entry.update(contracts=contracts, status='partial' if warns else 'complete',
                scanned_at=attempted, last_attempt=attempted, warnings=warns,
                cohort='rotation' if symbol in rotating else 'prescreened')
            entry['best_by_weight'] = best_by_weight(contracts)
            report['contract_audit'].extend(contracts)
            if not warns:
                scanned_rows.extend(contracts)
        except Exception as exc:
            entry.update(status='unavailable', last_attempt=attempted, warnings=[str(exc)])
            report['warnings'].append(f'{symbol} full scan: {exc}')
        report['stage3'].append(dict(ticker=symbol, status=entry['status'],
            cohort='rotation' if symbol in rotating else 'prescreened',
            contracts=len(entry['contracts']), warnings=entry.get('warnings', [])))
        completed_rotating += symbol in rotating
    _, state['scan_cursor'] = rotate(set(eligible)-set(primary), completed_rotating, state.get('scan_cursor', 0))
    report['stage3_deferred'] = order[len(report['stage3']):]
    if report['stage2_deferred'] or report['stage3_deferred']:
        report['warnings'].append('Scan budget reached; deferred names are listed; coverage is incomplete')
    report['missed_opportunities'] = missed_opportunities(scanned_rows, primary, rotating, config.material_improvement)
    report['rolling_ranking'] = rolling_ranking(cache, time.time(), config, set(eligible))
    report['eligible_symbols'] = eligible
    return report
