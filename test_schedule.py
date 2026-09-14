from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import unittest

from scan_schedule import next_events, slot_key, CRON

NY = ZoneInfo('America/New_York')


class ScheduleTests(unittest.TestCase):
    def next_scan(self, value):
        return next_events(datetime.fromisoformat(value).replace(tzinfo=NY))['scan'].astimezone(NY)

    def test_market_hours_quarter_hour(self):
        self.assertEqual(self.next_scan('2026-09-14T09:00').isoformat(), '2026-09-14T09:37:00-04:00')
        self.assertEqual(self.next_scan('2026-09-14T09:38').isoformat(), '2026-09-14T09:52:00-04:00')
        self.assertEqual(self.next_scan('2026-09-14T10:08').minute, 22)

    def test_weekend_and_exchange_holiday(self):
        self.assertEqual(self.next_scan('2026-09-04T16:40').isoformat(), '2026-09-08T09:37:00-04:00')

    def test_early_close_and_settlement(self):
        now = datetime(2026,11,27,13,1,tzinfo=NY)
        events = next_events(now)
        self.assertEqual(events['scan'].astimezone(NY).isoformat(), '2026-11-30T09:37:00-05:00')
        self.assertEqual(events['settlement'].astimezone(NY).isoformat(), '2026-11-27T13:22:00-05:00')

    def test_dst_uses_exchange_hours(self):
        target = self.next_scan('2026-10-30T16:40')
        self.assertEqual(target.isoformat(), '2026-11-02T09:37:00-05:00')
        self.assertEqual(target.astimezone(ZoneInfo('UTC')).hour, 14)

    def test_idempotency_slots_allow_faster_schedule(self):
        a = slot_key(datetime(2026,9,14,10,7,tzinfo=NY))
        b = slot_key(datetime(2026,9,14,10,22,tzinfo=NY))
        self.assertNotEqual(a, b)
        self.assertEqual(b, slot_key(datetime(2026,9,14,10,29,tzinfo=NY)))
        self.assertEqual(a, '2026-09-14T10:00')

    def test_display_matches_actual_workflow(self):
        workflow = Path(__file__).with_name('.github')/'workflows/paper.yml'
        self.assertIn(f"cron: '{CRON}'", workflow.read_text())


if __name__ == '__main__':
    unittest.main()
