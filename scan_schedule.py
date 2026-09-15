"""Expected GitHub trigger times, filtered through the NYSE session calendar."""
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import pandas_market_calendars as mcal

INTERVAL_MINUTES = 30
TRIGGER_MINUTES = (7, 37)
CRON = '7,37 13-21 * * 1-5'


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
