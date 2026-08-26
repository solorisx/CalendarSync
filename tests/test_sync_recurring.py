#!/usr/bin/env python3
"""
Recurring-event tests.

Layer 1 here: RRULE/RDATE/EXDATE mapping fidelity against the REAL icalendar library
(no network, no fakes). Proves the Google<->iCal recurrence round-trip that Phase 4b's
sync directions depend on. Behavior tests (extended fakes) live alongside the ping-pong
suite as the recurring sync is wired in.

Run:  python3 tests/test_sync_recurring.py
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sync_calendars import CalendarSync
from icalendar import Event, Calendar


def _vevent():
    ev = Event()
    ev.add('summary', 'Standup')
    ev.add('dtstart', datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc))
    ev.add('dtend', datetime(2026, 3, 1, 10, 30, tzinfo=timezone.utc))
    return ev


def _roundtrip(recurrence_list):
    ev = _vevent()
    CalendarSync._apply_google_recurrence(ev, recurrence_list)
    return CalendarSync._extract_ical_recurrence(ev)


def test_rrule_roundtrip_basic():
    assert _roundtrip(["RRULE:FREQ=WEEKLY;BYDAY=MO,WE"]) == ["RRULE:FREQ=WEEKLY;BYDAY=MO,WE"]


def test_rrule_with_exdate_roundtrip():
    src = ["RRULE:FREQ=DAILY;COUNT=3", "EXDATE:20260302T100000Z"]
    assert _roundtrip(src) == src


def test_exdate_with_tzid_param_preserved():
    src = ["RRULE:FREQ=WEEKLY;BYDAY=TU", "EXDATE;TZID=America/New_York:20260303T090000"]
    assert _roundtrip(src) == src, "TZID param on EXDATE must survive the round-trip"


def test_rdate_roundtrip():
    src = ["RRULE:FREQ=MONTHLY;BYMONTHDAY=15", "RDATE:20260401T120000Z"]
    assert _roundtrip(src) == src


def test_all_day_exdate_roundtrip():
    src = ["RRULE:FREQ=YEARLY", "EXDATE:20270101"]
    assert _roundtrip(src) == src


def test_apply_produces_real_rrule_property():
    ev = _vevent()
    CalendarSync._apply_google_recurrence(ev, ["RRULE:FREQ=WEEKLY;BYDAY=FR"])
    # The VEVENT must carry a real RRULE property that serializes into the ICS.
    ics = ev.to_ical().decode()
    assert "RRULE:FREQ=WEEKLY;BYDAY=FR" in ics
    assert ev.get('rrule') is not None


def test_empty_recurrence_is_noop():
    ev = _vevent()
    CalendarSync._apply_google_recurrence(ev, [])
    assert CalendarSync._extract_ical_recurrence(ev) == []


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t(); print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1; print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1; print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
