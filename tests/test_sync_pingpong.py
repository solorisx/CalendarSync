#!/usr/bin/env python3
"""
Regression tests for state-handling sync bugs (duplicate / update ping-pong).

These drive the REAL CalendarSync logic through in-memory fakes for the Google
Calendar API and the iCloud CalDAV surface. No network, no credentials.

Run:  python3 tests/test_sync_pingpong.py     (plain, no pytest needed)
      python3 -m pytest tests/test_sync_pingpong.py

What they lock in:
  * a mirrored event does NOT flap forever, regardless of whether iCloud
    re-stamps LAST-MODIFIED on our writes (the ping-pong bug);
  * iCloud-origin events stay stable;
  * GENUINE edits on either side still propagate exactly once (the fix must
    not over-suppress real changes);
  * _cleanup_past_events never drops an event still inside the active window
    (the re-seed-near-event-time bug).
"""
import os, sys, itertools
from datetime import datetime, timezone, timedelta
from icalendar import Calendar, Event

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sync_calendars
from sync_calendars import CalendarSync
import logging
sync_calendars.logger.setLevel(logging.ERROR)

_base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_counter = itertools.count()
def tick():     return _base + timedelta(seconds=next(_counter))
def tick_iso(): return tick().isoformat().replace("+00:00", "Z")
def at(sec):    return (_base + timedelta(seconds=sec)).isoformat().replace("+00:00", "Z")


# ----------------------------- fakes ---------------------------------------
class _Exec:
    def __init__(self, v): self._v = v
    def execute(self):     return self._v

class FakeGoogleEvents:
    def __init__(self, store): self.store = store; self.last_time_min = None; self.last_time_max = None
    def list(self, calendarId=None, timeMin=None, timeMax=None, singleEvents=None,
             orderBy=None, pageToken=None, iCalUID=None, showDeleted=None, **kwargs):
        if timeMin is not None: self.last_time_min = timeMin
        if timeMax is not None: self.last_time_max = timeMax
        items = list(self.store.values())
        if iCalUID is not None:
            items = [e for e in items if e.get("iCalUID") == iCalUID]
        return _Exec({"items": items, "nextPageToken": None})
    def insert(self, calendarId=None, body=None):
        gid = "gid_" + (body.get("iCalUID", "").split("@")[0] or str(next(_counter)))
        ev = dict(body); ev["id"] = gid
        ev["iCalUID"] = body.get("iCalUID") or (gid + "@google.com")
        ev["updated"] = tick_iso(); self.store[gid] = ev
        return _Exec(dict(ev))
    def patch(self, calendarId=None, eventId=None, body=None):
        ev = self.store[eventId]; ev.update(body); ev["updated"] = tick_iso()
        return _Exec(dict(ev))
    def delete(self, calendarId=None, eventId=None):
        self.store.pop(eventId, None); return _Exec({})

class FakeGoogleService:
    def __init__(self, store): self._e = FakeGoogleEvents(store)
    def events(self): return self._e

class FakeICloudEvent:
    def __init__(self, cal, uid, data): self._cal, self._uid, self.data = cal, uid, data
    def save(self):   self._cal._store(self.data, bump=self._cal.bump_on_update)
    def delete(self): self._cal.store.pop(self._uid, None)

class FakeICloudCalendar:
    name = "Home"
    def __init__(self, bump_on_update=True, emit_last_modified=True):
        self.store = {}
        self.bump_on_update = bump_on_update
        self.emit_last_modified = emit_last_modified
    def _store(self, ical_data, bump=True):
        cal = Calendar.from_ical(ical_data); uid = None
        for comp in cal.walk():
            if comp.name == "VEVENT":
                uid = str(comp.get("uid"))
                if self.emit_last_modified and (bump or comp.get("last-modified") is None):
                    comp.pop("last-modified", None); comp.add("last-modified", tick())
                elif not self.emit_last_modified:
                    comp.pop("last-modified", None)
                self.store[uid] = cal.to_ical()
        return uid
    def save_event(self, ical_data): return self._store(ical_data, bump=True)
    def date_search(self, start=None, end=None, expand=True):
        return [FakeICloudEvent(self, u, d) for u, d in list(self.store.items())]
    # test helpers -----------------------------------------------------------
    def summary_of(self, uid):
        cal = Calendar.from_ical(self.store[uid])
        for c in cal.walk():
            if c.name == "VEVENT":
                return str(c.get("summary"))
    def user_edit(self, uid, summary):
        cal = Calendar.from_ical(self.store[uid])
        for c in cal.walk():
            if c.name == "VEVENT":
                c.pop("summary", None); c.add("summary", summary)
                c.pop("last-modified", None); c.add("last-modified", tick())
        self.store[uid] = cal.to_ical()

class SandboxSync(CalendarSync):
    def __init__(self, g, i):
        self._google = FakeGoogleService(g); self._icloud = i
        self.config = {"google_calendar_id": "primary", "icloud": {"calendar_name": "Home"}}
        self.state = {"last_sync": None, "synced_events": {}, "last_error": None,
                      "last_notification_sent": None}
    def load_config(self): return self.config
    def load_state(self):  return self.state
    def save_state(self):  pass
    def get_google_service(self):  return self._google
    def get_icloud_calendar(self): return self._icloud
    def send_notification(self, *a, **k): pass


def gevent(start, summary="Team Meeting", updated=None):
    return {"id": "gid_evt1abc", "iCalUID": "gid_evt1abc@google.com", "summary": summary,
            "start": {"dateTime": start.isoformat().replace("+00:00", "Z")},
            "end":   {"dateTime": (start + timedelta(hours=1)).isoformat().replace("+00:00", "Z")},
            "updated": updated or tick_iso()}

def icloud_ics(uid, start, summary="Dentist", end=None):
    cal = Calendar(); ev = Event()
    ev.add("summary", summary); ev.add("uid", uid)
    ev.add("dtstart", start); ev.add("dtend", end or (start + timedelta(hours=1)))
    ev.add("last-modified", tick()); cal.add_component(ev)
    return cal.to_ical()

def cycle(sync):
    g = sync.sync_google_to_icloud(sync._google, sync._icloud)
    i = sync.sync_icloud_to_google(sync._google, sync._icloud)
    return g, i

START = datetime(2026, 3, 1, 10, tzinfo=timezone.utc)


# ----------------------------- tests ---------------------------------------
def test_no_pingpong_google_origin_bumping_server():
    """Aggressive iCloud (re-stamps LAST-MODIFIED on our writes) must not flap."""
    g = {"gid_evt1abc": gevent(START)}
    ic = FakeICloudCalendar(bump_on_update=True, emit_last_modified=True)
    s = SandboxSync(g, ic)
    gr, ir = cycle(s)
    assert gr["added"] == 1 and ir["added"] == 0, "first cycle should mirror once"
    for _ in range(6):
        gr, ir = cycle(s)
        assert (gr["added"], gr["updated"], gr["deleted"]) == (0, 0, 0), gr
        assert (ir["added"], ir["updated"], ir["deleted"]) == (0, 0, 0), ir


def test_no_pingpong_regardless_of_server_behaviour():
    for bump in (True, False):
        for emit in (True, False):
            g = {"gid_evt1abc": gevent(START)}
            ic = FakeICloudCalendar(bump_on_update=bump, emit_last_modified=emit)
            s = SandboxSync(g, ic)
            cycle(s)  # initial mirror
            for _ in range(5):
                gr, ir = cycle(s)
                assert gr["updated"] == 0 and ir["updated"] == 0, \
                    f"flap with bump={bump} emit={emit}: {gr} {ir}"


def test_icloud_origin_event_stable():
    g = {}
    ic = FakeICloudCalendar(bump_on_update=True, emit_last_modified=True)
    ic._store(icloud_ics("dentist-uid-1", START))
    s = SandboxSync(g, ic)
    gr, ir = cycle(s)
    assert ir["added"] == 1, "iCloud event should be added to Google once"
    for _ in range(5):
        gr, ir = cycle(s)
        assert gr["updated"] == 0 and ir["updated"] == 0, f"{gr} {ir}"


def test_genuine_google_edit_propagates_once():
    g = {"gid_evt1abc": gevent(START)}
    ic = FakeICloudCalendar(bump_on_update=True, emit_last_modified=True)
    s = SandboxSync(g, ic)
    cycle(s); cycle(s)  # reach steady state
    # user edits the event in Google
    g["gid_evt1abc"]["summary"] = "Team Meeting (moved)"
    g["gid_evt1abc"]["updated"] = at(9000)
    gr, ir = cycle(s)
    assert gr["updated"] == 1, f"Google edit should propagate to iCloud: {gr}"
    assert ic.summary_of("gid_evt1abc") == "Team Meeting (moved)"
    # and then settle
    gr, ir = cycle(s)
    assert gr["updated"] == 0 and ir["updated"] == 0, f"should settle: {gr} {ir}"


def test_genuine_icloud_edit_propagates_once():
    g = {}
    ic = FakeICloudCalendar(bump_on_update=False, emit_last_modified=True)
    ic._store(icloud_ics("dentist-uid-1", START))
    s = SandboxSync(g, ic)
    cycle(s); cycle(s)  # reach steady state
    ic.user_edit("dentist-uid-1", "Dentist (rescheduled)")
    gr, ir = cycle(s)
    assert ir["updated"] == 1, f"iCloud edit should propagate to Google: {ir}"
    gid = next(iter(g))
    assert g[gid]["summary"] == "Dentist (rescheduled)"
    gr, ir = cycle(s)
    assert gr["updated"] == 0 and ir["updated"] == 0, f"should settle: {gr} {ir}"


def test_cleanup_keeps_events_inside_window():
    """_cleanup_past_events must not drop an event still inside the sync window
    (start within the last day), which previously re-seeded change detection."""
    s = SandboxSync({}, FakeICloudCalendar())
    now = datetime.now(timezone.utc)
    s.state["synced_events"] = {
        "recent": {"title": "yesterday", "source": "google",
                   "start": (now - timedelta(hours=20)).isoformat()},
        "old":    {"title": "long past", "source": "google",
                   "start": (now - timedelta(days=5)).isoformat()},
    }
    s._cleanup_past_events()
    assert "recent" in s.state["synced_events"], "must keep event still in window"
    assert "old" not in s.state["synced_events"], "must drop event well past window"


def test_state_atomic_save_and_load_roundtrip():
    import tempfile
    sf = os.path.join(tempfile.mkdtemp(), "sync_state.json")
    old = sync_calendars.STATE_FILE
    sync_calendars.STATE_FILE = sf
    try:
        obj = CalendarSync.__new__(CalendarSync)
        obj.state = {"last_sync": "x", "synced_events": {"a": {"title": "t"}},
                     "last_error": None, "last_notification_sent": None}
        obj.save_state()
        assert os.path.exists(sf)
        loaded = CalendarSync.load_state(obj)
        assert loaded["synced_events"]["a"]["title"] == "t"
        # missing keys in an older file are backfilled
        for k in ("last_sync", "synced_events", "last_error", "last_notification_sent"):
            assert k in loaded
    finally:
        sync_calendars.STATE_FILE = old


def test_state_recovers_from_corruption():
    import tempfile
    sf = os.path.join(tempfile.mkdtemp(), "sync_state.json")
    old = sync_calendars.STATE_FILE
    sync_calendars.STATE_FILE = sf
    try:
        obj = CalendarSync.__new__(CalendarSync)
        obj.state = {"last_sync": None, "synced_events": {"good": {}},
                     "last_error": None, "last_notification_sent": None}
        obj.save_state()  # writes primary
        obj.state = {"last_sync": None, "synced_events": {"good2": {}},
                     "last_error": None, "last_notification_sent": None}
        obj.save_state()  # rotates previous primary -> .bak
        with open(sf, "w") as f:
            f.write("{ this is not valid json")  # corrupt primary
        loaded = CalendarSync.load_state(obj)
        assert "good" in loaded["synced_events"], f"should recover from backup: {loaded}"
    finally:
        sync_calendars.STATE_FILE = old


def test_sync_window_env_and_config_precedence():
    from datetime import datetime as _dt, timezone as _tz
    obj = CalendarSync.__new__(CalendarSync)
    fixed = _dt(2026, 1, 1, tzinfo=_tz.utc)
    old_past, old_future = sync_calendars.SYNC_PAST_DAYS, sync_calendars.SYNC_FUTURE_DAYS
    try:
        sync_calendars.SYNC_PAST_DAYS = 1
        sync_calendars.SYNC_FUTURE_DAYS = 1825
        # env/module defaults
        obj.config = {}
        now, start_dt, end_dt, tmin, tmax = obj._sync_window(now=fixed)
        assert (fixed - start_dt).days == 1
        assert (end_dt - fixed).days == 1825, (end_dt - fixed).days
        # config.json overrides env
        obj.config = {"sync_past_days": 3, "sync_future_days": 30}
        _, start_dt, end_dt, _, _ = obj._sync_window(now=fixed)
        assert (fixed - start_dt).days == 3 and (end_dt - fixed).days == 30
    finally:
        sync_calendars.SYNC_PAST_DAYS, sync_calendars.SYNC_FUTURE_DAYS = old_past, old_future


def test_queries_use_configured_future_horizon():
    """Prove every hardcoded +360d literal was replaced: the Google query's timeMax
    must sit ~5y out (default), not ~1y. A far-future one-off is no longer clipped."""
    from datetime import datetime as _dt, timezone as _tz
    old_future = sync_calendars.SYNC_FUTURE_DAYS
    try:
        sync_calendars.SYNC_FUTURE_DAYS = 1825
        g = {"gid_evt1abc": gevent(START)}
        ic = FakeICloudCalendar(bump_on_update=True, emit_last_modified=True)
        s = SandboxSync(g, ic)
        cycle(s)
        tmax = s._google._e.last_time_max
        horizon = (_parse := __import__("sync_calendars")._parse_instant)(tmax)
        days_out = (horizon - _dt.now(_tz.utc)).days
        assert days_out > 1000, f"future horizon only {days_out}d out — literal not replaced"
    finally:
        sync_calendars.SYNC_FUTURE_DAYS = old_future


def gevent_allday(d, summary="Holiday"):
    return {"id": "gid_allday1", "iCalUID": "gid_allday1@google.com", "summary": summary,
            "start": {"date": d.isoformat()},
            "end": {"date": (d + timedelta(days=1)).isoformat()},
            "updated": tick_iso()}


def test_all_day_event_stays_all_day():
    from datetime import date as _date, datetime as _dt
    g = {"gid_allday1": gevent_allday(_date(2026, 12, 25))}
    ic = FakeICloudCalendar()
    s = SandboxSync(g, ic)
    cycle(s)
    cal = Calendar.from_ical(ic.store["gid_allday1"])
    dtstart = next(c.get("dtstart").dt for c in cal.walk() if c.name == "VEVENT")
    # All-day must be a bare date, NOT a midnight datetime (the old corruption).
    assert isinstance(dtstart, _date) and not isinstance(dtstart, _dt), f"corrupted to {dtstart!r}"
    assert dtstart == _date(2026, 12, 25)


def test_hashed_uid_event_deletes_from_icloud():
    """A Google event whose UID is hashed (len > 200) must be deletable from iCloud:
    the delete loop must match on the stored (hashed) icloud_uid, not the Google id."""
    import hashlib
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    long_id = "g" * 250
    start = _dt.now(_tz.utc) + _td(days=2)  # in-window relative to real now
    g = {long_id: {"id": long_id, "iCalUID": long_id + "@google.com", "summary": "Long",
                   "start": {"dateTime": start.isoformat().replace("+00:00", "Z")},
                   "end": {"dateTime": (start + _td(hours=1)).isoformat().replace("+00:00", "Z")},
                   "updated": tick_iso()}}
    ic = FakeICloudCalendar()
    s = SandboxSync(g, ic)
    cycle(s)
    expected_uid = hashlib.sha256(long_id.encode()).hexdigest()
    assert expected_uid in ic.store, "event should be created in iCloud under the hashed uid"
    assert s.state["synced_events"][long_id].get("icloud_uid") == expected_uid
    del g[long_id]  # user deletes it in Google
    gr, ir = cycle(s)
    assert expected_uid not in ic.store, "hashed-uid event must be deleted, not orphaned"
    assert long_id not in s.state["synced_events"]


def test_cleanup_ages_out_on_event_end_not_start():
    """Cleanup must age entries out on when the event ENDS. A long event whose
    start has slipped behind the window edge is still live — Google's timeMin
    matches on end time and keeps returning it — so dropping its entry would
    strip the marker that says 'already synced'."""
    s = SandboxSync({}, FakeICloudCalendar())
    now = datetime.now(timezone.utc)
    s.state["synced_events"] = {
        "running":  {"title": "started, still running", "source": "icloud",
                     "start": (now - timedelta(days=4)).isoformat(),
                     "end":   (now + timedelta(days=6)).isoformat()},
        "finished": {"title": "long event, now over", "source": "icloud",
                     "start": (now - timedelta(days=20)).isoformat(),
                     "end":   (now - timedelta(days=9)).isoformat()},
        "legacy":   {"title": "entry written before 'end' existed", "source": "google",
                     "start": (now - timedelta(days=30)).isoformat()},
    }
    s._cleanup_past_events()
    kept = s.state["synced_events"]
    assert "running" in kept, "must keep a long event that is still running"
    assert "finished" not in kept, "must drop an event that has fully ended"
    assert "legacy" not in kept, "entries with no 'end' still age out on start"


def test_long_running_event_not_duplicated_back_into_icloud():
    """Regression for the 'Preparation time' duplicate: an iCloud event whose
    start has passed but which is still running gets mirrored to Google, and must
    never come back the other way as a second iCloud copy — cycle after cycle,
    with cleanup running in between exactly as run_sync does it."""
    now = datetime.now(timezone.utc)
    ic = FakeICloudCalendar()
    ic._store(icloud_ics("apple-uid-long", now - timedelta(days=2),
                         summary="Preparation time", end=now + timedelta(days=9)))
    s = SandboxSync({}, ic)

    gr, ir = cycle(s)
    assert ir["added"] == 1, f"should mirror to Google once: {ir}"

    for n in range(4):
        s._cleanup_past_events()
        gr, ir = cycle(s)
        assert gr["added"] == 0, f"cycle {n}: written back into iCloud: {gr}"
        assert ir["added"] == 0, f"cycle {n}: re-added to Google: {ir}"
    assert len(ic.store) == 1, f"expected one iCloud copy, got {list(ic.store)}"


def test_icloud_origin_not_duplicated_after_state_loss():
    """Belt and braces for the same bug: even with the state entry gone entirely,
    the forward pass must recognise the event it pushed to Google by its foreign
    iCalUID rather than deriving a fresh UID from Google's unrelated event id."""
    ic = FakeICloudCalendar()
    ic._store(icloud_ics("apple-uid-2", START))
    s = SandboxSync({}, ic)
    cycle(s)
    assert len(ic.store) == 1, "setup: one copy after the first mirror"

    s.state["synced_events"] = {}          # state pruned / lost
    gr, ir = cycle(s)
    assert len(ic.store) == 1, f"duplicate written back into iCloud: {list(ic.store)}"
    assert gr["added"] == 0, gr


def test_legacy_icloud_uid_spelling_is_preserved():
    """Events existing installs already wrote under the id-derived UID must keep
    that spelling — switching derivation must not orphan them into duplicates."""
    g = {"gid_evt1abc": gevent(START)}
    ic = FakeICloudCalendar()
    s = SandboxSync(g, ic)
    cycle(s)
    assert "gid_evt1abc" in ic.store, f"google-native event keeps its UID: {list(ic.store)}"

    # An ICS-imported Google event carrying a foreign iCalUID, already mirrored
    # into iCloud under the legacy spelling.
    g["gid_imported"] = {"id": "gid_imported", "iCalUID": "imported-from-ics",
                         "summary": "Imported",
                         "start": {"dateTime": START.isoformat().replace("+00:00", "Z")},
                         "end": {"dateTime": (START + timedelta(hours=1)).isoformat().replace("+00:00", "Z")},
                         "updated": tick_iso()}
    ic._store(icloud_ics("gid_imported", START, summary="Imported"))
    before = set(ic.store)
    cycle(s)
    assert set(ic.store) == before, f"legacy spelling must be reused, not duplicated: {set(ic.store) - before}"


def test_oneway_mirrored_event_never_pushed_back_to_google():
    """A one-way mirrored event must never be pushed into the primary Google
    calendar, even with no state entry vouching for it — losing that entry is
    what let the mirror leak back into Google in the first place."""
    ic = FakeICloudCalendar()
    mirrored_uid = "ow-de9daec6dee4-g4ac5ilmstg45j5re2v7qu72ek"
    ic._store(icloud_ics(mirrored_uid, START, summary="Sascha at LOCATION"))
    g = {}
    s = SandboxSync(g, ic)
    s.state["synced_events"] = {}          # state lost / pruned

    gr, ir = cycle(s)
    assert ir["added"] == 0, f"mirrored event pushed back to Google: {ir}"
    assert g == {}, f"primary Google calendar was written to: {list(g)}"

    # ...while an ordinary iCloud event beside it still syncs normally.
    ic._store(icloud_ics("4A6E8C8E-EFC4-4E2E-915C-AD053A93D25E", START, summary="Dentist"))
    gr, ir = cycle(s)
    assert ir["added"] == 1, f"ordinary iCloud event must still sync: {ir}"
    assert len(g) == 1, f"exactly one event should reach Google: {list(g)}"


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        global _counter, _base
        _counter = itertools.count()  # reset clock per test for determinism
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
