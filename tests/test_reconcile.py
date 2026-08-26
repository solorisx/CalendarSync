#!/usr/bin/env python3
"""
Regression tests for reconcile.py's event matching.

reconcile is an audit: its only job is to tell you when the two calendars have
actually drifted. It used to re-derive the Google→iCloud UID mapping by hand,
with rules simpler than the sync's own, so every event whose two sides spell the
UID differently was reported as BOTH "Google only" AND "iCloud only" — on the
live install that was 36 of the ~38 reported in each bucket, which buried the
handful of real diffs.

These drive the REAL reconcile() through the same in-memory fakes as the sync
tests. No network, no credentials.

Run:  python3 tests/test_reconcile.py
"""
import os, sys, itertools
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
import sync_calendars
sync_calendars.logger.setLevel(logging.CRITICAL)
import reconcile
reconcile.logger.setLevel(logging.CRITICAL)
from sync_calendars import CalendarSync

from test_sync_pingpong import (FakeGoogleService, FakeICloudCalendar, SandboxSync,
                                icloud_ics, cycle, tick_iso)

START = datetime.now(timezone.utc) + timedelta(days=10)
CONFIG = {"google_calendar_id": "primary", "icloud": {"calendar_name": "Home"}}


def gevent(gid, summary="Team Meeting", ical_uid=None, start=None):
    start = start or START
    return {"id": gid, "iCalUID": ical_uid or (gid + "@google.com"), "summary": summary,
            "start": {"dateTime": start.isoformat().replace("+00:00", "Z")},
            "end": {"dateTime": (start + timedelta(hours=1)).isoformat().replace("+00:00", "Z")},
            "updated": tick_iso()}


def run_reconcile(google_store, icloud_cal, state):
    """Drive the real reconcile() against the fakes."""
    saved = (CalendarSync.load_config, CalendarSync.load_state,
             reconcile.get_google_service, reconcile.get_icloud_calendar)
    CalendarSync.load_config = lambda self: CONFIG
    CalendarSync.load_state = lambda self: state
    gsvc = (google_store if isinstance(google_store, FakeGoogleService)
            else FakeGoogleService(google_store))
    reconcile.get_google_service = lambda: gsvc
    reconcile.get_icloud_calendar = lambda config: icloud_cal
    try:
        return reconcile.reconcile()
    finally:
        (CalendarSync.load_config, CalendarSync.load_state,
         reconcile.get_google_service, reconcile.get_icloud_calendar) = saved


def _synced(google_store, icloud_cal):
    """Let the real sync mirror the events, so state/UIDs are what they'd really be."""
    s = SandboxSync(google_store, icloud_cal)
    s.config = dict(CONFIG)
    cycle(s); cycle(s)
    return s.state


# ----------------------------- tests ---------------------------------------
def test_underscore_id_event_matches_instead_of_double_counting():
    """A Google id beginning '_' is stripped for iCloud, so the two sides spell the
    UID differently. That is one event, not one missing from each calendar."""
    g = {"_gid_underscore1": gevent("_gid_underscore1", ical_uid="_gid_underscore1@google.com")}
    ic = FakeICloudCalendar(bump_on_update=False, emit_last_modified=True)
    state = _synced(g, ic)
    assert list(ic.store) == ["gid_underscore1"], f"precondition: {list(ic.store)}"

    r = run_reconcile(g, ic, state)
    assert len(r["only_in_google"]) == 0, f"double-counted in Google: {r['only_in_google']}"
    assert len(r["only_in_icloud"]) == 0, f"double-counted in iCloud: {r['only_in_icloud']}"
    assert len(r["in_both_match"]) == 1, f"should match as one event: {r['in_both_match']}"


def test_adopted_foreign_ical_uid_matches():
    """An imported event carries a foreign iCalUID; the sync may keep the id-derived
    spelling in iCloud instead. reconcile must follow the sync's choice, not guess."""
    g = {"gid_imported": gevent("gid_imported", summary="Imported", ical_uid="imported-from-ics")}
    ic = FakeICloudCalendar(bump_on_update=False, emit_last_modified=True)
    # Already present in iCloud under the LEGACY id-derived spelling.
    ic._store(icloud_ics("gid_imported", START, summary="Imported"))
    state = _synced(g, ic)
    assert set(ic.store) == {"gid_imported"}, f"precondition: {set(ic.store)}"

    r = run_reconcile(g, ic, state)
    assert len(r["only_in_google"]) == 0 and len(r["only_in_icloud"]) == 0, \
        f"foreign-iCalUID event double-counted: {r['only_in_google']} {r['only_in_icloud']}"
    assert len(r["in_both_match"]) == 1


def test_mirrored_events_are_not_reported_as_icloud_only():
    """One-way mirrored copies live only in iCloud by design; counting them as drift
    is noise the README has to apologise for."""
    ic = FakeICloudCalendar()
    ic._store(icloud_ics("ow-de9daec6dee4-g4ac5ilmstg45j5re2v7qu72ek", START, summary="Sascha at X"))
    state = {"last_sync": None, "synced_events": {}, "last_error": None,
             "last_notification_sent": None}

    r = run_reconcile({}, ic, state)
    assert len(r["only_in_icloud"]) == 0, f"mirrored copy counted as drift: {r['only_in_icloud']}"
    assert len(r["mirrored_only"]) == 1, f"should be bucketed as mirrored: {r['mirrored_only']}"


def test_real_drift_is_still_reported():
    """The whole point: after silencing the noise, genuine diffs must still surface."""
    g = {"gid_only_google": gevent("gid_only_google", summary="Google Only")}
    ic = FakeICloudCalendar()
    ic._store(icloud_ics("icloud-only-uid-1", START, summary="iCloud Only"))
    state = {"last_sync": None, "synced_events": {}, "last_error": None,
             "last_notification_sent": None}

    r = run_reconcile(g, ic, state)
    assert len(r["only_in_google"]) == 1, f"real Google-only diff lost: {r['only_in_google']}"
    assert len(r["only_in_icloud"]) == 1, f"real iCloud-only diff lost: {r['only_in_icloud']}"


def test_content_mismatch_still_detected():
    g = {"gid_evt1abc": gevent("gid_evt1abc", summary="Team Meeting")}
    ic = FakeICloudCalendar(bump_on_update=False, emit_last_modified=True)
    state = _synced(g, ic)
    # Drift the iCloud copy behind the sync's back.
    ic.user_edit("gid_evt1abc", "Something Else Entirely")

    r = run_reconcile(g, ic, state)
    assert len(r["in_both_mismatch"]) == 1, f"content drift missed: {r['in_both_mismatch']}"
    assert len(r["only_in_google"]) == 0 and len(r["only_in_icloud"]) == 0


def test_timed_event_in_a_non_utc_zone_is_not_drift():
    """The sync writes UTC into iCloud while Google keeps the event's own zone, so the
    same moment reads 17:47+02:00 on one side and 15:47 on the other. Comparing the
    wall-clock hour called every such event drift."""
    zurich = timezone(timedelta(hours=2))
    start = (datetime.now(timezone.utc) + timedelta(days=20)).astimezone(zurich).replace(
        hour=17, minute=47, second=0, microsecond=0)
    g = {"gid_tz1": gevent("gid_tz1", summary="Abendessen", start=start)}
    ic = FakeICloudCalendar(bump_on_update=False, emit_last_modified=True)
    state = _synced(g, ic)

    r = run_reconcile(g, ic, state)
    assert len(r["in_both_mismatch"]) == 0, \
        f"same instant reported as drift: {[m[3] for m in r['in_both_mismatch']]}"
    assert len(r["in_both_match"]) == 1


def test_genuine_time_change_is_still_reported():
    """Having silenced the timezone noise, a real reschedule must still surface."""
    g = {"gid_tz2": gevent("gid_tz2", summary="Abendessen")}
    ic = FakeICloudCalendar(bump_on_update=False, emit_last_modified=True)
    state = _synced(g, ic)
    # Move the Google side an hour later, behind the sync's back.
    moved = START + timedelta(hours=1)
    g["gid_tz2"]["start"] = {"dateTime": moved.isoformat().replace("+00:00", "Z")}
    g["gid_tz2"]["end"] = {"dateTime": (moved + timedelta(hours=1)).isoformat().replace("+00:00", "Z")}

    r = run_reconcile(g, ic, state)
    assert len(r["in_both_mismatch"]) == 1, f"real reschedule missed: {r['in_both_mismatch']}"


def test_all_day_event_is_not_drift():
    """All-day events cross the same code path and must not become instants."""
    day = (datetime.now(timezone.utc) + timedelta(days=15)).date()
    g = {"gid_allday": {"id": "gid_allday", "iCalUID": "gid_allday@google.com",
                        "summary": "Feiertag",
                        "start": {"date": day.isoformat()},
                        "end": {"date": (day + timedelta(days=1)).isoformat()},
                        "updated": tick_iso()}}
    ic = FakeICloudCalendar(bump_on_update=False, emit_last_modified=True)
    state = _synced(g, ic)

    r = run_reconcile(g, ic, state)
    assert len(r["in_both_mismatch"]) == 0, \
        f"all-day event reported as drift: {[m[3] for m in r['in_both_mismatch']]}"
    assert len(r["in_both_match"]) == 1


def test_midnight_utc_event_collapses_on_both_sides():
    """An event ending 02:00+02:00 in Google is the same 00:00Z iCloud stores naively.
    Collapsing midnight before converting collapsed only one side, inventing drift."""
    zurich = timezone(timedelta(hours=2))
    end = (datetime.now(timezone.utc) + timedelta(days=12)).astimezone(zurich).replace(
        hour=2, minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=5)
    g = {"gid_mid": {"id": "gid_mid", "iCalUID": "gid_mid@google.com", "summary": "Nachtclub",
                     "start": {"dateTime": start.isoformat()},
                     "end": {"dateTime": end.isoformat()},
                     "updated": tick_iso()}}
    ic = FakeICloudCalendar(bump_on_update=False, emit_last_modified=True)
    state = _synced(g, ic)

    r = run_reconcile(g, ic, state)
    assert len(r["in_both_mismatch"]) == 0, \
        f"midnight-UTC end reported as drift: {[m[3] for m in r['in_both_mismatch']]}"


def test_window_follows_config_not_just_env():
    """reconcile kept its own env-only window, so an install that set the horizon in
    config.json got false diffs for the band the sync manages but this did not scan."""
    global CONFIG
    saved, gsvc = CONFIG, FakeGoogleService({"gid_evt1abc": gevent("gid_evt1abc")})
    CONFIG = dict(saved, sync_future_days=7)
    try:
        run_reconcile(gsvc, FakeICloudCalendar(),
                      {"last_sync": None, "synced_events": {}, "last_error": None,
                       "last_notification_sent": None})
    finally:
        CONFIG = saved

    horizon = datetime.fromisoformat(gsvc._e.last_time_max.replace("Z", "+00:00"))
    days = (horizon - datetime.now(timezone.utc)).days
    assert 5 <= days <= 8, f"config horizon ignored, queried {days} days ahead"


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
