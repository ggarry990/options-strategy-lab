"""Deterministic cash-secured put experiment. No broker/order API exists here."""
from __future__ import annotations
import copy
import math
from scoring import score_candidate
from datetime import datetime, date, timezone

VERSION = 2
CAPITAL = 100_000.0
FEE = 1.0
STRATEGIES = {
    'A': dict(name='Hold to expiry', minimum=75),
    'B': dict(name='Take 25% profit', minimum=75, target=.25),
    'C': dict(name='Take 50% profit', minimum=75, target=.50),
    'D': dict(name='Take 75% profit', minimum=75, target=.75),
    'E': dict(name='50% profit or 7 DTE', minimum=75, target=.50, time_exit=7),
    'F': dict(name='50% profit + loss limit', minimum=75, target=.50, stop=3),
    'G': dict(name='35 points ahead of expected', minimum=75, ahead=.35),
    'H': dict(name='Selective 100 / 50% profit', minimum=100, target=.50),
    'I': dict(name='Selective 125 / 50% profit', minimum=125, target=.50),
    'J': dict(name='Defensive 100 / 25% / 14 DTE', minimum=100, target=.25, stop=2, time_exit=14),
}
for _cfg in STRATEGIES.values():
    _cfg['return_weight'] = 30
# Same entry threshold and exit as A; only the entry weight differs.
for _weight in (40, 50, 60):
    STRATEGIES[f'A{_weight}'] = dict(STRATEGIES['A'], return_weight=_weight,
        name=f'Hold to expiry | {_weight}% return / {100-_weight}% protection')


def migrate_state(state, now):
    """Add portfolios and metadata, never reconstruct existing balances/history."""
    if state.get('version') not in (1, VERSION):
        raise ValueError('Unknown state version; refusing to reset portfolios')
    models = state.get('models', {})
    if not set('ABCDEFGHIJ').issubset(models) or set(models) - set(STRATEGIES):
        raise ValueError('Missing original or unknown portfolios; refusing to reset')
    required = {'cash', 'positions', 'closed', 'events', 'history', 'fees', 'peak', 'max_drawdown'}
    if any(not required.issubset(m) for m in models.values()):
        raise ValueError('Incomplete portfolio; refusing to reset')
    result = copy.deepcopy(state)
    new_models = fresh_state(now)['models']
    added = []
    for key in STRATEGIES:
        if key not in models:
            result['models'][key] = new_models[key]
            added.append(key)
    if state['version'] != VERSION or added:
        result.setdefault('migrations', []).append(dict(time=now, from_version=state['version'],
            to_version=VERSION, added_models=added, note='Existing portfolios unchanged; broader entry pipeline begins now'))
    result['version'] = VERSION
    result.setdefault('pipeline_started', now)
    result.setdefault('option_cache', {})
    return result

def fresh_state(now):
    return dict(version=VERSION, created=now, last_slot=None, last_success=None,
                models={k: dict(created=now, cash=CAPITAL, positions=[], closed=[], events=[], history=[], fees=0., peak=CAPITAL, max_drawdown=0.) for k in STRATEGIES}, runs=[])

def valid_number(value):
    return isinstance(value, (float, int)) and math.isfinite(value)

def expected_capture(p, today):
    # Entry-spot / entry-IV Black-Scholes decay, normalized to theoretical entry price.
    def price(days):
        s, k, v = p['spot'], p['strike'], p['iv']
        t = max(days, 0) / 365
        if t <= 0:
            return max(k-s, 0.)
        d1 = (math.log(s/k)+(.04+v*v/2)*t)/(v*math.sqrt(t))
        d2 = d1-v*math.sqrt(t)
        cdf = lambda x: .5*(1+math.erf(x/math.sqrt(2)))
        return k*math.exp(-.04*t)*cdf(-d2)-s*cdf(-d1)
    if not valid_number(p.get('iv')) or p['iv'] <= 0:
        return None
    p0 = price((date.fromisoformat(p['expiry'])-date.fromisoformat(p['entered'][:10])).days)
    if p0 <= 1e-10:
        return None
    return 1-price((date.fromisoformat(p['expiry'])-today).days)/p0

def exit_reason(cfg, p, ask, today):
    captured = 1-ask/p['credit']
    dte = (date.fromisoformat(p['expiry'])-today).days
    if cfg.get('stop') and ask >= cfg['stop']*p['credit']:
        return f"Loss limit: buyback {cfg['stop']}x entry credit"
    if cfg.get('target') and captured >= cfg['target']:
        return f"Profit target: {captured:.1%} premium captured"
    if cfg.get('time_exit') and dte <= cfg['time_exit']:
        return f"Time exit: {dte} days remaining"
    expected = expected_capture(p, today)
    if cfg.get('ahead') and expected is not None and captured > 0 and captured-expected >= cfg['ahead']:
        return f"Ahead of theoretical decay by {(captured-expected)*100:.1f} percentage points"
    return None

def run_cycle(state, candidates, quotes, settlements, now, slot, allow_entries=True):
    """quotes are validated exact-contract asks; settlements are expiry-session closes.

    A failed quote is NEVER zero. Retain last known liability and label it stale.
    Cash includes reserved collateral; available cash subtracts strike*100.
    Expiry uses cash-equivalent intrinsic settlement, not physical share assignment.
    """
    if state.get('last_slot') == slot:
        return state
    state = copy.deepcopy(state)
    state['entry_audit'] = []
    today = date.fromisoformat(now[:10])
    for key, model in state['models'].items():
        cfg = STRATEGIES[key]
        touched = set()
        for p in list(model['positions']):
            reason, cost, fee = None, None, FEE
            expired = date.fromisoformat(p['expiry']) < today or p['contract'] in settlements
            if expired:
                spot = settlements.get(p['contract'])
                if valid_number(spot) and spot > 0:
                    cost = max(p['strike']-spot, 0.)
                    reason, fee = 'Expiry: cash-equivalent intrinsic settlement', 0.
                    p['settlement_spot'] = spot
            else:
                q = quotes.get(p['contract'])
                if q and valid_number(q.get('ask')) and q['ask'] > 0:
                    p['mark'], p['mark_time'] = q['ask'], now
                    reason = exit_reason(cfg, p, q['ask'], today)
                    cost = q['ask'] if reason else None
            if reason:
                model['cash'] -= cost*100+fee
                model['fees'] += fee
                pnl = (p['credit']-cost)*100-FEE-fee
                model['closed'].append(dict(p, exited=now, exit_price=cost, pnl=pnl, reason=reason))
                model['positions'].remove(p)
                touched.add(p['ticker'])
                model['events'].append(dict(time=now, action='CLOSE', ticker=p['ticker'], contract=p['contract'], reason=reason, pnl=pnl))
            else:
                fresh = p.get('mark_time') == now
                reason = 'Hold: no exit threshold reached' if fresh else 'Hold: fresh quote / expiry close unavailable'
                model['events'].append(dict(time=now, action='HOLD', ticker=p['ticker'], contract=p['contract'], reason=reason))
        ranked = [dict(c, score=score_candidate(c, cfg['return_weight']), return_weight=cfg['return_weight']) for c in candidates]
        for rank, c in enumerate(sorted(ranked, key=lambda x: (-x['score'], x['contract'])), 1):
            reasons = list(c.get('rejections', []))
            if not allow_entries:
                reasons.append('entry window closed')
            if len(model['positions']) >= 5:
                reasons.append('concentration: five-position limit')
            held = {p['ticker'] for p in model['positions']}
            reserve = sum(p['strike']*100 for p in model['positions'])
            required = c['strike']*100
            if c['ticker'] in held:
                reasons.append('concentration: ticker already held')
            if c['ticker'] in touched:
                reasons.append('closed this cycle')
            if c['score'] < cfg['minimum']:
                reasons.append('score below strategy minimum')
            if not (0 < required <= CAPITAL*.20):
                reasons.append('collateral: above position limit')
            if model['cash']-reserve-required-FEE < CAPITAL*.10:
                reasons.append('collateral: cash reserve')
            if c['dte'] <= cfg.get('time_exit', 0):
                reasons.append('DTE at exit threshold')
            state['entry_audit'].append(dict(strategy=key, rank=rank, ticker=c['ticker'],
                contract=c['contract'], score=c['score'], return_weight=cfg['return_weight'],
                decision='rejected' if reasons else 'selected', reasons=reasons))
            if reasons:
                continue
            p = dict(c, entered=now, credit=c['bid'], mark=c['ask'], mark_time=now)
            model['positions'].append(p)
            model['cash'] += c['bid']*100-FEE
            model['fees'] += FEE
            model['events'].append(dict(time=now, action='OPEN', ticker=c['ticker'], contract=c['contract'], reason=f"Opportunity Index {c['score']:.1f} >= {cfg['minimum']}; highest eligible rank", credit=c['bid']))
        liability = sum(p['mark']*100 for p in model['positions'])
        nav = model['cash']-liability
        stale = sum(p.get('mark_time') != now for p in model['positions'])
        model['peak'] = max(model['peak'], nav)
        dd = nav/model['peak']-1
        model['max_drawdown'] = min(model['max_drawdown'], dd)
        model['history'].append(dict(time=now, nav=nav, stale=stale, drawdown=dd))
        model['events'] = model['events'][-5000:]
    state['last_slot'], state['last_success'] = slot, now
    return state
