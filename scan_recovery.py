"""Durable retry bookkeeping; missing data is never a negative stock score."""
import re


def coverage_history(state, symbols, now, freshness_minutes):
    rows = []
    for ticker in sorted(set(symbols)):
        entry = state.get('option_cache', {}).get(ticker, {})
        stamp = entry.get('last_complete_at')
        if stamp is None and entry.get('status') == 'complete':
            stamp = entry.get('scanned_at')  # Additive compatibility with old cache.
        age = max(0, (now-stamp)/60) if stamp is not None else None
        rows.append(dict(ticker=ticker, last_complete_at=stamp, age_minutes=age,
            status=entry.get('status', 'never scanned'),
            overdue=age is None or age > freshness_minutes,
            outstanding_expiries=[r['expiry'] for r in entry.get('expiry_audit', [])
                                 if r['status'] not in ('complete', 'empty_confirmed')]))
    return sorted(rows, key=lambda r: (r['last_complete_at'] is not None, r['last_complete_at'] or 0, r['ticker']))


def prioritize_blind_spots(state, symbols, limit, now, freshness_minutes):
    return [r['ticker'] for r in coverage_history(state, symbols, now, freshness_minutes)
            if r['overdue'] and not retry_waiting(state, r['ticker'], 'stage3', now)][:limit]


def record_retry(state, ticker, stage, error, now, attempted=True):
    queue = state.setdefault('scan_retries', {})
    key = f'{stage}:{ticker}'
    row = queue.setdefault(key, dict(ticker=ticker, stage=stage, first_failed=now,
        attempts=0, last_attempt=0, next_retry_at=now))
    if attempted or 'error' not in row:
        row['error'] = error
    else:
        row['deferred_reason'] = error
    if attempted:
        row['attempts'] += 1
        row['last_attempt'] = now
        # 30, 60, 120, then 240 minutes. No repeated hammering within a run.
        row['next_retry_at'] = now+min(240, 30*2**min(row['attempts']-1, 3))*60
    return row


def clear_retry(state, ticker, stage):
    state.setdefault('scan_retries', {}).pop(f'{stage}:{ticker}', None)


def due_retries(state, stage, eligible, now, limit):
    rows = [r for r in state.get('scan_retries', {}).values()
            if r['stage'] == stage and r['ticker'] in eligible and r['next_retry_at'] <= now]
    rows.sort(key=lambda r: (r['last_attempt'], r['first_failed'], r['ticker']))
    return [r['ticker'] for r in rows[:limit]]


def retry_waiting(state, ticker, stage, now):
    row = state.get('scan_retries', {}).get(f'{stage}:{ticker}')
    return row is not None and row['next_retry_at'] > now


def seed_previous_failures(state, now):
    if state.get('recovery_version') == 1:
        return
    for r in state.get('last_audit', {}).get('stage2', []):
        if r.get('status') != 'checked':
            record_retry(state, r['ticker'], 'stage2', r.get('error', 'Previous ATM check incomplete'), now, attempted=False)
    if not state.get('last_audit', {}).get('stage2'):
        # An after-close run has no ATM rows. Recover the last scan's failures
        # from its saved warnings rather than forgetting them overnight.
        latest_scan = next((r for r in reversed(state.get('runs', [])) if r.get('stage2_checked')), {})
        for warning in latest_scan.get('warnings', []):
            match = re.match(r'^([A-Z0-9-]+) ATM: (.+)$', warning)
            if match:
                record_retry(state, match[1], 'stage2', match[2], now, attempted=False)
    for ticker, r in state.get('option_cache', {}).items():
        if r.get('status') != 'complete':
            record_retry(state, ticker, 'stage3', 'Previous full scan incomplete', now, attempted=False)
    state['recovery_version'] = 1


def coverage_gate(report, config):
    s2 = len(report['stage2_planned'])
    s3 = len(report['stage3_planned'])
    good2 = sum(r.get('status') == 'checked' for r in report['stage2'])
    good3 = sum(r.get('status') == 'complete' for r in report['stage3'])
    ratio2, ratio3 = good2/s2 if s2 else 0., good3/s3 if s3 else 0.
    allowed = ratio2 >= config.min_stage2_coverage and ratio3 >= config.min_stage3_coverage
    return dict(allowed=allowed, stage2_successful=good2, stage2_planned=s2,
        stage3_complete=good3, stage3_planned=s3, stage2_ratio=ratio2, stage3_ratio=ratio3,
        reason='' if allowed else 'New entries paused: insufficient completed scan coverage')
