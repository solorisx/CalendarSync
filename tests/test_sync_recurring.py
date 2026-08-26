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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # tests dir (sibling imports)
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


from datetime import timedelta
from test_sync_pingpong import FakeICloudCalendar, SandboxSync, cycle


def _icloud_master_ics(uid, summary, recurrence_lines, start):
    cal = Calendar(); ev = Event()
    ev.add('summary', summary); ev.add('uid', uid)
    ev.add('dtstart', start); ev.add('dtend', start + timedelta(hours=1))
    CalendarSync._apply_google_recurrence(ev, recurrence_lines)
    cal.add_component(ev)
    return cal.to_ical()


def _one_icloud_vevent(ic):
    cal = Calendar.from_ical(next(iter(ic.store.values())))
    return next(c for c in cal.walk() if c.name == "VEVENT")


def test_icloud_recurring_series_creates_ONE_google_master():
    """The headline fix: a weekly iCloud series must become ONE recurring Google event
    (carrying recurrence[]), not ~52 fanned-out singles."""
    start = datetime(2026, 3, 2, 9, tzinfo=timezone.utc)
    g = {}
    ic = FakeICloudCalendar()
    ic._store(_icloud_master_ics("icloud-weekly-1", "Standup",
                                 ["RRULE:FREQ=WEEKLY;BYDAY=MO"], start))
    s = SandboxSync(g, ic)
    cycle(s)
    assert len(g) == 1, f"expected ONE Google master, got {len(g)}: {list(g)}"
    ev = next(iter(g.values()))
    assert any("RRULE" in r for r in ev.get("recurrence", [])), ev.get("recurrence")


def test_google_recurring_master_creates_ONE_icloud_vevent():
    start = datetime(2026, 3, 2, 9, tzinfo=timezone.utc)
    g = {"gid_daily": {"id": "gid_daily", "iCalUID": "gid_daily@google.com",
                       "summary": "Daily", "recurrence": ["RRULE:FREQ=DAILY;COUNT=5"],
                       "start": {"dateTime": start.isoformat().replace("+00:00", "Z")},
                       "end": {"dateTime": (start + timedelta(hours=1)).isoformat().replace("+00:00", "Z")},
                       "updated": "2026-01-01T00:00:00Z"}}
    ic = FakeICloudCalendar()
    s = SandboxSync(g, ic)
    cycle(s)
    assert len(ic.store) == 1, f"expected ONE iCloud VEVENT, got {len(ic.store)}"
    ve = _one_icloud_vevent(ic)
    assert ve.get("rrule") is not None, "iCloud VEVENT must carry RRULE"


def test_recurring_no_pingpong_icloud_origin():
    start = datetime(2026, 3, 2, 9, tzinfo=timezone.utc)
    g = {}
    ic = FakeICloudCalendar(bump_on_update=True, emit_last_modified=True)
    ic._store(_icloud_master_ics("icloud-weekly-2", "Standup",
                                 ["RRULE:FREQ=WEEKLY"], start))
    s = SandboxSync(g, ic)
    cycle(s)  # initial mirror
    for _ in range(4):
        gr, ir = cycle(s)
        assert gr["updated"] == 0 and ir["updated"] == 0, f"recurring ping-pong: {gr} {ir}"
    assert len(g) == 1


def test_icloud_exdate_propagates_to_google():
    """An occurrence cancelled in iCloud (EXDATE on the master) must reach Google's
    recurrence[] — the user's real cancellation path."""
    start = datetime(2026, 3, 2, 9, tzinfo=timezone.utc)
    g = {}
    ic = FakeICloudCalendar()
    ic._store(_icloud_master_ics("icloud-weekly-3", "Standup",
                                 ["RRULE:FREQ=WEEKLY", "EXDATE:20260309T090000Z"], start))
    s = SandboxSync(g, ic)
    cycle(s)
    ev = next(iter(g.values()))
    rec = ev.get("recurrence", [])
    assert any("RRULE" in r for r in rec) and any("EXDATE" in r for r in rec), rec


def _gmaster(gid, start, recurrence, updated="2026-01-01T00:00:00Z"):
    return {gid: {"id": gid, "iCalUID": gid + "@google.com", "summary": "Standup",
                  "recurrence": recurrence,
                  "start": {"dateTime": start.isoformat().replace("+00:00", "Z")},
                  "end": {"dateTime": (start + timedelta(hours=1)).isoformat().replace("+00:00", "Z")},
                  "updated": updated}}


def _cancelled_exception(ex_id, parent, raw):
    return {ex_id: {"id": ex_id, "status": "cancelled", "recurringEventId": parent,
                    "originalStartTime": {"dateTime": raw}}}


def test_google_cancelled_occurrence_becomes_exdate_in_icloud():
    start = datetime(2026, 3, 2, 9, tzinfo=timezone.utc)
    g = {}
    g.update(_gmaster("gid_weekly", start, ["RRULE:FREQ=WEEKLY"]))
    g.update(_cancelled_exception("gid_weekly_ex1", "gid_weekly", "2026-03-09T09:00:00Z"))
    ic = FakeICloudCalendar()
    s = SandboxSync(g, ic)
    cycle(s)
    ve = _one_icloud_vevent(ic)
    assert ve.get("rrule") is not None
    assert ve.get("exdate") is not None, "cancelled Google occurrence must become an EXDATE"


def test_google_later_cancellation_propagates_via_recurrence_sig():
    """Cancelling an occurrence may not bump the master's `updated`; the recurrence
    signature must still catch it and propagate the new EXDATE."""
    start = datetime(2026, 3, 2, 9, tzinfo=timezone.utc)
    g = _gmaster("gid_weekly2", start, ["RRULE:FREQ=WEEKLY"])
    ic = FakeICloudCalendar()
    s = SandboxSync(g, ic)
    cycle(s)
    assert _one_icloud_vevent(ic).get("exdate") is None
    # user cancels one occurrence in Google (master.updated unchanged)
    g.update(_cancelled_exception("gid_weekly2_ex1", "gid_weekly2", "2026-03-09T09:00:00Z"))
    gr, ir = cycle(s)
    assert gr["updated"] == 1, "recurrence change should trigger an iCloud update"
    assert _one_icloud_vevent(ic).get("exdate") is not None


def test_migration_flags_legacy_fanout_only():
    loaded = {"synced_events": {
        "uid_2026-03-02T09:00:00+00:00": {"title": "Standup (recurring)", "source": "icloud"},
        "normal-1": {"title": "Lunch", "source": "icloud"},
    }}
    CalendarSync._migrate_fanout(loaded)
    assert loaded["synced_events"]["uid_2026-03-02T09:00:00+00:00"].get("legacy_fanout") is True
    assert "legacy_fanout" not in loaded["synced_events"]["normal-1"]


def test_legacy_fanout_single_not_auto_deleted_or_repushed():
    """A leftover fan-out single in Google must be neither auto-deleted (manual cleanup)
    nor re-pushed to iCloud."""
    key = "uid_2026-03-02T09:00:00+00:00"
    g = {"gid_x": {"id": "gid_x", "iCalUID": key, "summary": "Standup (recurring)",
                   "start": {"dateTime": "2026-03-02T09:00:00Z"},
                   "end": {"dateTime": "2026-03-02T09:30:00Z"}, "updated": "2026-01-01T00:00:00Z"}}
    ic = FakeICloudCalendar()
    s = SandboxSync(g, ic)
    s.state["synced_events"][key] = {"title": "Standup (recurring)", "source": "icloud",
                                     "start": "2026-03-02T09:00:00+00:00",
                                     "icloud_uid": key, "legacy_fanout": True}
    cycle(s)
    assert "gid_x" in g, "legacy fan-out single must NOT be auto-deleted from Google"
    assert len(ic.store) == 0, "legacy fan-out single must NOT be re-pushed to iCloud"


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
