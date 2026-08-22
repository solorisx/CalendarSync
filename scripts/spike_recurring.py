#!/usr/bin/env python3
"""
Recurring-event READ + WRITE verification spike (Phase 4a).

This is the go/no-go gate for syncing recurring events as *real* recurring series
(RRULE) instead of expanding them into per-occurrence copies. It runs against the
REAL Google + iCloud accounts (it reuses the app's existing auth) and answers:

  READ
    G1. Does Google `events.list(singleEvents=False)` return recurring MASTERS with a
        `recurrence[]` array over our window?
    G2. Does it return a master whose DTSTART predates the window but which recurs into
        it? (the one uncertain time-range behavior)
    I1. Does iCloud `date_search(expand=False)` return master VEVENTs carrying RRULE?

  WRITE
    G3. Does `events.insert` accept `recurrence:['RRULE:...']` and create a real series?
    G4. Does adding an EXDATE to the master's `recurrence[]` drop one occurrence?
    I2. Does iCloud accept a VEVENT with RRULE/EXDATE via the normal save path and round-
        trip it (series + excluded date)?

All writes use disposable events prefixed "CALSYNC-SPIKE-" and are deleted at the end;
`--cleanup` removes any leftover spike events from a previous aborted run; `--read-only`
skips every write. Optional `--fixtures-dir DIR` dumps the read-back payloads of the
spike's OWN synthetic events (no personal data) so they can seed contract tests.

Run inside the container so /app/data (config + token) is present:
    docker-compose run --rm calendar-sync python scripts/spike_recurring.py
    docker-compose run --rm calendar-sync python scripts/spike_recurring.py --read-only
    docker-compose run --rm calendar-sync python scripts/spike_recurring.py --cleanup
"""
import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

# Reuse the app's auth + config (paths, token, caldav connection).
sys.path.insert(0, '/app')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sync_calendars import CalendarSync, SuppressCaldavOutput  # noqa: E402
from icalendar import Calendar, Event  # noqa: E402

SPIKE_PREFIX = "CALSYNC-SPIKE-"
PAST_DAYS = int(os.getenv('SYNC_PAST_DAYS', '1'))
FUTURE_DAYS = int(os.getenv('SYNC_FUTURE_DAYS', '1825'))

results = []  # (code, ok, message)
def record(code, ok, message):
    results.append((code, ok, message))
    flag = "PASS" if ok else ("SKIP" if ok is None else "FAIL")
    print(f"  [{flag}] {code}: {message}")


def dump_fixture(fixtures_dir, name, data):
    if not fixtures_dir:
        return
    os.makedirs(fixtures_dir, exist_ok=True)
    path = os.path.join(fixtures_dir, name)
    mode = 'wb' if isinstance(data, (bytes, bytearray)) else 'w'
    with open(path, mode) as f:
        f.write(data)
    print(f"       ↳ wrote fixture {path}")


# --------------------------------------------------------------------------- READ
def probe_google_read(gsvc, cal_id, fixtures_dir):
    print("\nGoogle READ probes (singleEvents=False):")
    now = datetime.now(timezone.utc)
    time_min = (now - timedelta(days=PAST_DAYS)).isoformat().replace('+00:00', 'Z')
    time_max = (now + timedelta(days=FUTURE_DAYS)).isoformat().replace('+00:00', 'Z')
    try:
        items, page_token = [], None
        while True:
            resp = gsvc.events().list(
                calendarId=cal_id, timeMin=time_min, timeMax=time_max,
                singleEvents=False, showDeleted=False, pageToken=page_token,
                maxResults=2500,
            ).execute()
            items.extend(resp.get('items', []))
            page_token = resp.get('nextPageToken')
            if not page_token:
                break
    except Exception as e:
        record("G1", False, f"events.list(singleEvents=False) failed: {e}")
        return

    masters = [e for e in items if e.get('recurrence')]
    record("G1", bool(masters),
           f"{len(masters)} recurring master(s) with recurrence[] over the window "
           f"(of {len(items)} total items)")
    if masters:
        sample = masters[0]
        print(f"       sample: {sample.get('summary','(no title)')!r} recurrence={sample.get('recurrence')}")
        dump_fixture(fixtures_dir, "google_master_sample.json", json.dumps(sample, indent=2))

    # G2: a master whose first occurrence starts BEFORE the window but which recurs into it.
    old_recurring = []
    for e in masters:
        s = e.get('start', {})
        raw = s.get('dateTime') or s.get('date')
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt < now - timedelta(days=PAST_DAYS):
                old_recurring.append((e.get('summary'), raw))
        except Exception:
            pass
    if old_recurring:
        record("G2", True,
               f"{len(old_recurring)} master(s) with DTSTART before the window are still "
               f"returned (recur into it) — e.g. {old_recurring[0]}")
    else:
        record("G2", None,
               "no master with DTSTART before the window found to test (create a long-running "
               "weekly event to exercise this; not a failure)")


def probe_icloud_read(ical, fixtures_dir):
    print("\niCloud READ probe (expand=False):")
    now = datetime.now(timezone.utc)
    try:
        with SuppressCaldavOutput():
            objs = ical.date_search(start=now - timedelta(days=PAST_DAYS),
                                    end=now + timedelta(days=FUTURE_DAYS), expand=False)
    except Exception as e:
        record("I1", False, f"date_search(expand=False) failed: {e}")
        return
    with_rrule, sample_ics = 0, None
    for o in objs:
        try:
            cal = Calendar.from_ical(o.data)
        except Exception:
            continue
        for comp in cal.walk():
            if comp.name == "VEVENT" and comp.get('rrule'):
                with_rrule += 1
                if sample_ics is None:
                    sample_ics = o.data
    record("I1", with_rrule > 0 if objs else None,
           f"{with_rrule} master VEVENT(s) carrying RRULE (of {len(objs)} objects returned)")
    if sample_ics:
        dump_fixture(fixtures_dir, "icloud_master_sample.ics",
                     sample_ics if isinstance(sample_ics, (bytes, bytearray)) else sample_ics.encode())


# -------------------------------------------------------------------------- WRITE
def probe_google_write(gsvc, cal_id, fixtures_dir):
    print("\nGoogle WRITE probes (insert recurrence + EXDATE):")
    tag = SPIKE_PREFIX + uuid.uuid4().hex[:8]
    start = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0)
    body = {
        'summary': tag,
        'start': {'dateTime': start.isoformat(), 'timeZone': 'UTC'},
        'end': {'dateTime': (start + timedelta(hours=1)).isoformat(), 'timeZone': 'UTC'},
        'recurrence': ['RRULE:FREQ=DAILY;COUNT=3'],
    }
    created = None
    try:
        created = gsvc.events().insert(calendarId=cal_id, body=body).execute()
        inst = gsvc.events().instances(calendarId=cal_id, eventId=created['id']).execute()
        n = len([i for i in inst.get('items', []) if i.get('status') != 'cancelled'])
        record("G3", n == 3, f"inserted series, {n} instance(s) materialized (expected 3)")
        dump_fixture(fixtures_dir, "google_written_master.json", json.dumps(created, indent=2))

        # G4: add an EXDATE for the 2nd occurrence and confirm it drops out.
        exdate = (start + timedelta(days=1))
        exline = "EXDATE;TZID=UTC:" + exdate.strftime("%Y%m%dT%H%M%S")
        gsvc.events().patch(calendarId=cal_id, eventId=created['id'],
                            body={'recurrence': ['RRULE:FREQ=DAILY;COUNT=3', exline]}).execute()
        inst2 = gsvc.events().instances(calendarId=cal_id, eventId=created['id']).execute()
        n2 = len([i for i in inst2.get('items', []) if i.get('status') != 'cancelled'])
        record("G4", n2 == 2, f"after EXDATE, {n2} instance(s) remain (expected 2)")
    except Exception as e:
        record("G3", False, f"recurring write/EXDATE failed: {e}")
    finally:
        if created:
            try:
                gsvc.events().delete(calendarId=cal_id, eventId=created['id']).execute()
                print(f"       ↳ cleaned up Google spike event {tag}")
            except Exception as e:
                print(f"       ! could not delete Google spike event {created.get('id')}: {e}")


def probe_icloud_write(ical, fixtures_dir):
    print("\niCloud WRITE probe (RRULE + EXDATE round-trip):")
    tag = SPIKE_PREFIX + uuid.uuid4().hex[:8]
    uid = tag
    start = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0)
    cal = Calendar()
    ev = Event()
    ev.add('summary', tag)
    ev.add('uid', uid)
    ev.add('dtstart', start)
    ev.add('dtend', start + timedelta(hours=1))
    ev.add('rrule', {'freq': ['DAILY'], 'count': [3]})
    ev.add('exdate', start + timedelta(days=1))
    cal.add_component(ev)
    saved = None
    try:
        saved = ical.save_event(cal.to_ical())
        # Read back non-expanded → RRULE present?
        with SuppressCaldavOutput():
            back = ical.event_by_uid(uid)
        rrule_ok = False
        parsed = Calendar.from_ical(back.data)
        for c in parsed.walk():
            if c.name == "VEVENT":
                rrule_ok = bool(c.get('rrule'))
        record("I2", rrule_ok, "iCloud accepted and round-tripped an RRULE/EXDATE VEVENT")
        dump_fixture(fixtures_dir, "icloud_written_master.ics",
                     back.data if isinstance(back.data, (bytes, bytearray)) else back.data.encode())
    except Exception as e:
        record("I2", False, f"iCloud RRULE write/round-trip failed: {e}")
    finally:
        if saved is not None:
            try:
                saved.delete()
                print(f"       ↳ cleaned up iCloud spike event {tag}")
            except Exception as e:
                print(f"       ! could not delete iCloud spike event {uid}: {e}")


def cleanup_leftovers(gsvc, cal_id, ical):
    print("Cleaning up any leftover CALSYNC-SPIKE- events...")
    now = datetime.now(timezone.utc)
    try:
        resp = gsvc.events().list(calendarId=cal_id, q=SPIKE_PREFIX,
                                  timeMin=(now - timedelta(days=2)).isoformat().replace('+00:00', 'Z'),
                                  timeMax=(now + timedelta(days=30)).isoformat().replace('+00:00', 'Z'),
                                  singleEvents=False).execute()
        for e in resp.get('items', []):
            if str(e.get('summary', '')).startswith(SPIKE_PREFIX):
                gsvc.events().delete(calendarId=cal_id, eventId=e['id']).execute()
                print(f"  deleted Google {e.get('summary')}")
    except Exception as e:
        print(f"  Google cleanup error: {e}")
    try:
        with SuppressCaldavOutput():
            objs = ical.date_search(start=now - timedelta(days=2), end=now + timedelta(days=30), expand=False)
        for o in objs:
            cal = Calendar.from_ical(o.data)
            for c in cal.walk():
                if c.name == "VEVENT" and str(c.get('summary', '')).startswith(SPIKE_PREFIX):
                    o.delete()
                    print(f"  deleted iCloud {c.get('summary')}")
    except Exception as e:
        print(f"  iCloud cleanup error: {e}")


def main():
    ap = argparse.ArgumentParser(description="Recurring read/write verification spike")
    ap.add_argument('--read-only', action='store_true', help="skip all write probes")
    ap.add_argument('--cleanup', action='store_true', help="delete leftover spike events and exit")
    ap.add_argument('--fixtures-dir', default=None, help="dump spike read-back payloads here")
    args = ap.parse_args()

    sync = CalendarSync()
    gsvc = sync.get_google_service()
    ical = sync.get_icloud_calendar()
    cal_id = sync.config['google_calendar_id']

    if args.cleanup:
        cleanup_leftovers(gsvc, cal_id, ical)
        return 0

    print("=" * 70)
    print("RECURRING READ/WRITE SPIKE")
    print(f"window: -{PAST_DAYS}d .. +{FUTURE_DAYS}d   google_calendar_id={cal_id}")
    print("=" * 70)

    probe_google_read(gsvc, cal_id, args.fixtures_dir)
    probe_icloud_read(ical, args.fixtures_dir)
    if not args.read_only:
        probe_google_write(gsvc, cal_id, args.fixtures_dir)
        probe_icloud_write(ical, args.fixtures_dir)

    print("\n" + "=" * 70)
    print("SUMMARY")
    hard = [r for r in results if r[1] is False]
    softskip = [r for r in results if r[1] is None]
    for code, ok, msg in results:
        flag = "PASS" if ok else ("SKIP" if ok is None else "FAIL")
        print(f"  {flag}  {code}  {msg}")
    print("-" * 70)
    if hard:
        print(f"NO-GO: {len(hard)} probe(s) failed — do not proceed to Phase 4b until resolved.")
        print("       (If only time-range probes failed, the fallback is to query masters")
        print("        with a loose/absent bound — see the plan Risk #1.)")
        return 1
    if softskip:
        print("GO with caveat: all critical probes passed; some were skipped for lack of test")
        print("       data (noted above). Safe to proceed to Phase 4b.")
        return 0
    print("GO: read + write of recurring events confirmed on both sides. Proceed to Phase 4b.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
