"""Expected GitHub trigger times, filtered through the NYSE session calendar."""
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import pandas_market_calendars as mcal

INTERVAL_MINUTES = 30
TRIGGER_MINUTES = (7, 37)
CRON = '7,37 13-21 * * 1-5'


def schedule_diagnostics(runs, now):
    """Compare observed launches with cron windows, without claiming trigger identity.

    GitHub exposes creation/start times, not the intended firing timestamp. A
    launch is associated only with its containing 30-minute cron window; an
    empty window could mean a missed trigger or one delayed into a later window.
    """
    now = now.astimezone(timezone.utc)
    valid = [r for r in runs if isinstance(r, dict) and r.get('created_at')]
    if not valid:
        return dict(available=False, windows=[], runs=[], reason='Workflow history unavailable')
    def parse(value):
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)
    # Restrict analysis to returned history, at most seven days. No extrapolation
    # before the oldest record if the public API result was truncated.
    start = max(min(parse(r['created_at']) for r in valid), now-timedelta(days=7))
    observed = []
    for r in valid:
        created = parse(r['created_at'])
        if created < start or created > now:
            continue
        started = parse(r['run_started_at']) if r.get('run_started_at') else None
        observed.append(dict(id=r.get('id'), created_at=created.isoformat(),
            event=r.get('event'), status=r.get('status'), conclusion=r.get('conclusion'),
            queue_delay_seconds=max(0, (started-created).total_seconds()) if started else None,
            url=r.get('html_url')))
    scheduled = [parse(r['created_at']) for r in valid if r.get('event') == 'schedule']
    windows = []
    day = start.date()
    while day <= now.date():
        if day.weekday() < 5:
            for hour in range(13, 22):
                for minute in TRIGGER_MINUTES:
                    target = datetime.combine(day, datetime.min.time(), timezone.utc).replace(hour=hour, minute=minute)
                    end = target+timedelta(minutes=INTERVAL_MINUTES)
                    if target < start or end+timedelta(minutes=10) > now:
                        continue
                    count = sum(target <= stamp < end for stamp in scheduled)
                    windows.append(dict(expected_at=target.isoformat(), observed_launches=count,
                        status='Observed launch' if count else 'No launch observed — missed or delayed'))
        day += timedelta(days=1)
    return dict(available=True, since=start.isoformat(), until=now.isoformat(),
        expected_windows=len(windows), unobserved_windows=sum(not w['observed_launches'] for w in windows),
        windows=windows, runs=observed)


def slot_key(now):
    """Keep existing timestamp keys; allow one portfolio cycle per half hour."""
    return now.strftime('%Y-%m-%dT%H:')+f'{now.minute//INTERVAL_MINUTES*INTERVAL_MINUTES:02d}'


def already_processed(previous, now):
    if not previous:
        return False
    try:
        # A saved :15/:45 key from the old frequency is still in this half hour.
        return slot_key(datetime.fromisoformat(previous)) == slot_key(now)
    except ValueError:
        return previous == slot_key(now)


@lru_cache(maxsize=32)
def sessions(start, end):
    return mcal.get_calendar('NYSE').schedule(start_date=start, end_date=end)


def next_events(now):
    if now.tzinfo is None:
        raise ValueError('An aware timestamp is required')
    now = now.astimezone(timezone.utc)
    schedule = sessions(now.date().isoformat(), (now.date()+timedelta(days=30)).isoformat())
    next_scan = next_settlement = None
    for session_day, row in schedule.iterrows():
        for hour in range(13, 22):
            for minute in TRIGGER_MINUTES:
                target = datetime.combine(session_day.date(), datetime.min.time(), timezone.utc).replace(hour=hour, minute=minute)
                if target <= now:
                    continue
                if next_scan is None and row['market_open'] <= target < row['market_close']:
                    next_scan = target
                if next_settlement is None and target >= row['market_close']+timedelta(minutes=20):
                    next_settlement = target
        if next_scan and next_settlement:
            break
    return {'scan':next_scan, 'settlement':next_settlement}
