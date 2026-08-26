#!/usr/bin/env python3
"""
Calendar Reconciliation Tool

Compares events between Google Calendar and iCloud to identify:
- Events present in Google but missing in iCloud
- Events present in iCloud but missing in Google
- Events present in both but with mismatched content (title, start, end)
- Sync state entries for events that no longer exist in either calendar

Run inside the Docker container:
    docker-compose run --rm calendar-sync python reconcile.py

Or locally (with venv activated):
    python reconcile.py
"""

import argparse
import os
import sys
import json
import pickle
import logging
from io import StringIO
from datetime import datetime, timedelta, timezone
from icalendar import Calendar
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import caldav

# ---------------------------------------------------------------------------
# Config / paths (same as sync_calendars.py)
# ---------------------------------------------------------------------------
# Data directory: the container mount point when it exists, otherwise the repo's
# own ./data so host runs (scripts, spikes, tests) work without extra setup.
# CALSYNC_DATA_DIR overrides both.
def _default_data_dir():
    if os.path.isdir('/app/data'):
        return '/app/data'
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')

DATA_DIR = os.path.expanduser(os.getenv('CALSYNC_DATA_DIR') or _default_data_dir())
CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')
TOKEN_FILE = os.path.join(DATA_DIR, 'token.pickle')
STATE_FILE = os.path.join(DATA_DIR, 'sync_state.json')
CREDENTIALS_FILE = os.path.join(DATA_DIR, 'credentials.json')

SCOPES = ['https://www.googleapis.com/auth/calendar']

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('googleapiclient').setLevel(logging.WARNING)
logging.getLogger('caldav').setLevel(logging.WARNING)


class SuppressCaldavOutput:
    def __enter__(self):
        self.old_stderr = sys.stderr
        sys.stderr = StringIO()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        sys.stderr = self.old_stderr
        return False


# ---------------------------------------------------------------------------
# Auth helpers (copied from CalendarSync)
# ---------------------------------------------------------------------------

def get_google_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        logger.debug(f"Loading Google OAuth token from {TOKEN_FILE}")
        with open(TOKEN_FILE, 'rb') as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.debug("Refreshing expired Google OAuth token")
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                logger.error(f"{CREDENTIALS_FILE} not found")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=8080, host='0.0.0.0')
        with open(TOKEN_FILE, 'wb') as f:
            pickle.dump(creds, f)

    logger.debug("Google Calendar service ready")
    return build('calendar', 'v3', credentials=creds)


def get_icloud_calendar(config):
    icloud_cfg = config['icloud']
    logger.debug(f"Connecting to iCloud CalDAV: {icloud_cfg['url']}")
    client = caldav.DAVClient(
        url=icloud_cfg['url'],
        username=icloud_cfg['username'],
        password=icloud_cfg['password'],
    )
    principal = client.principal()
    calendars = principal.calendars()
    logger.debug(f"Found {len(calendars)} iCloud calendars")

    calendar_name = icloud_cfg.get('calendar_name')
    if calendar_name:
        for cal in calendars:
            if cal.name == calendar_name:
                logger.debug(f"Using iCloud calendar: {cal.name}")
                return cal
        names = [c.name for c in calendars]
        raise Exception(f"Calendar '{calendar_name}' not found. Available: {names}")

    logger.debug(f"No calendar_name configured, using first calendar: {calendars[0].name}")
    return calendars[0]


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def fetch_google_events(service, calendar_id, time_min, time_max):
    """Fetch all Google events in range, paginated. Returns list of event dicts."""
    logger.debug(f"Fetching Google events from {time_min} to {time_max}")
    all_events = []
    page_token = None
    page = 0
    while True:
        page += 1
        result = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=False,  # compare recurring MASTERS, matching the sync engine
            pageToken=page_token,
        ).execute()
        items = result.get('items', [])
        logger.debug(f"  Google page {page}: {len(items)} events")
        all_events.extend(items)
        page_token = result.get('nextPageToken')
        if not page_token:
            break
    logger.debug(f"Total Google events fetched: {len(all_events)}")
    return all_events


def fetch_icloud_events(icloud_calendar, start, end):
    """Fetch all iCloud events in range. Returns list of (event_id, component) tuples."""
    logger.debug(f"Fetching iCloud events from {start.isoformat()} to {end.isoformat()}")
    with SuppressCaldavOutput():
        raw_events = icloud_calendar.date_search(start=start, end=end, expand=False)
    logger.debug(f"Total iCloud raw event objects fetched: {len(raw_events)}")

    parsed = []
    parse_errors = 0
    for raw in raw_events:
        try:
            ical = Calendar.from_ical(raw.data)
        except Exception as e:
            logger.error(f"Failed to parse iCloud event: {e}")
            parse_errors += 1
            continue

        for component in ical.walk():
            if component.name != 'VEVENT':
                continue

            # expand=False: one master per series. Skip RECURRENCE-ID overrides so the
            # comparison is master-to-master (matching the non-expanded sync engine).
            if component.get('recurrence-id'):
                continue
            event_id = str(component.get('uid', ''))

            parsed.append((event_id, component))

    logger.debug(f"Parsed {len(parsed)} iCloud VEVENT components ({parse_errors} parse errors)")
    return parsed


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def normalise_dt(dt_val):
    """Return an ISO string for comparison.

    Uses wall-clock time (stripping timezone info) so that Google's tz-aware
    times (e.g. 19:00+02:00) and iCloud's naive times (e.g. 19:00:00, which
    the sync wrote as UTC but iCloud returns without tz) compare as equal when
    the wall-clock hour matches.

    Naive datetimes at midnight are treated as date-only (iCloud sometimes
    returns all-day events as datetime(Y,M,D,0,0,0) instead of date(Y,M,D)).
    """
    if dt_val is None:
        return None
    if isinstance(dt_val, datetime):
        # Strip timezone — compare wall-clock hours only.
        # Both Google (tz-aware) and iCloud (naive, stored as UTC by sync but
        # returned without tz) will show the same local/display time this way.
        if dt_val.hour == 0 and dt_val.minute == 0 and dt_val.second == 0:
            # Treat midnight as date-only (handles all-day events stored as datetime)
            return dt_val.date().isoformat()
        return dt_val.strftime('%Y-%m-%dT%H:%M:%S')
    # date object
    return dt_val.isoformat()


def google_event_times(g_event):
    """Return (start_str, end_str) from a Google event dict."""
    # Google uses 'date' key for all-day events, 'dateTime' for timed events.
    # Preserve date-only strings as-is so they compare correctly with iCloud date objects.
    start_raw = g_event['start'].get('dateTime') or g_event['start'].get('date', '')
    end_raw = g_event['end'].get('dateTime') or g_event['end'].get('date', '')

    def parse(raw):
        if not raw:
            return raw
        # Date-only string (no 'T') — return as-is to match iCloud date objects
        if 'T' not in raw:
            return raw
        try:
            return normalise_dt(datetime.fromisoformat(raw.replace('Z', '+00:00')))
        except Exception:
            return raw

    return parse(start_raw), parse(end_raw)


def icloud_event_times(component):
    """Return (start_str, end_str) from an icalendar VEVENT component."""
    dtstart_prop = component.get('dtstart')
    dtend_prop = component.get('dtend')
    start = normalise_dt(dtstart_prop.dt if dtstart_prop else None)
    end = normalise_dt(dtend_prop.dt if dtend_prop else None)
    return start, end


# ---------------------------------------------------------------------------
# Main reconciliation logic
# ---------------------------------------------------------------------------

def reconcile():
    # Load config
    if not os.path.exists(CONFIG_FILE):
        logger.error(f"Config file not found: {CONFIG_FILE}")
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        config = json.load(f)
    logger.debug(f"Loaded config from {CONFIG_FILE}")

    # Load sync state
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)
        logger.debug(f"Loaded sync state: {len(state.get('synced_events', {}))} entries")
    else:
        logger.warning(f"No sync state file found at {STATE_FILE}")

    synced_events = state.get('synced_events', {})

    # Time window — must match the sync engine's window exactly, otherwise reconcile
    # reports false Google-only/iCloud-only diffs for the band the sync manages but
    # reconcile doesn't scan. Same env defaults as sync_calendars (SYNC_PAST_DAYS /
    # SYNC_FUTURE_DAYS). (Phase 5 will hoist these into a shared module.)
    past_days = int(os.getenv('SYNC_PAST_DAYS', '1'))
    future_days = int(os.getenv('SYNC_FUTURE_DAYS', '1825'))
    now = datetime.now(timezone.utc)
    start_dt = now - timedelta(days=past_days)
    end_dt = now + timedelta(days=future_days)
    time_min = start_dt.isoformat().replace('+00:00', 'Z')
    time_max = end_dt.isoformat().replace('+00:00', 'Z')

    logger.info("=" * 60)
    logger.info("Calendar Reconciliation")
    logger.info(f"Window: {time_min}  →  {time_max}")
    logger.info("=" * 60)

    # --- Fetch Google events ---
    logger.info("Fetching Google Calendar events...")
    google_service = get_google_service()
    google_events_list = fetch_google_events(
        google_service, config['google_calendar_id'], time_min, time_max
    )

    # Index Google events by a normalised canonical UID so they match iCloud's bare UIDs.
    # Google native events have iCalUID like "abc123@google.com"; iCloud stores them as "abc123".
    # We use the bare UID as the canonical key and keep a separate alias map for lookups.
    google_by_uid = {}       # canonical UID → event
    google_uid_aliases = {}  # raw iCalUID → canonical UID (for reporting the original UID)

    def canonical_uid(uid):
        uid = uid.lstrip('_')
        if '@google.com' in uid:
            uid = uid.split('@google.com')[0]
        return uid

    for ev in google_events_list:
        raw_uid = ev.get('iCalUID', ev['id'])
        canon = canonical_uid(raw_uid)
        google_by_uid[canon] = ev
        google_uid_aliases[raw_uid] = canon
        logger.debug(f"  Google event: {ev.get('summary', 'No Title')!r}  iCalUID={raw_uid}  canonical={canon}")

    logger.info(f"Google: {len(google_events_list)} events ({len(google_by_uid)} unique UIDs after normalisation)")

    # --- Fetch iCloud events ---
    logger.info("Fetching iCloud Calendar events...")
    icloud_calendar = get_icloud_calendar(config)
    icloud_events_list = fetch_icloud_events(icloud_calendar, start_dt, end_dt)

    icloud_by_uid = {}
    for event_id, component in icloud_events_list:
        icloud_by_uid[canonical_uid(event_id)] = component
    logger.info(f"iCloud: {len(icloud_events_list)} events ({len(icloud_by_uid)} unique UIDs)")

    # ---------------------------------------------------------------------------
    # Analysis
    # ---------------------------------------------------------------------------
    all_uids = set(google_by_uid.keys()) | set(icloud_by_uid.keys())
    logger.info(f"\nTotal distinct UIDs across both calendars: {len(all_uids)}")

    only_in_google = []
    only_in_icloud = []
    in_both_match = []
    in_both_mismatch = []

    for uid in sorted(all_uids):
        in_google = uid in google_by_uid
        in_icloud = uid in icloud_by_uid

        if in_google and not in_icloud:
            only_in_google.append(uid)
        elif in_icloud and not in_google:
            only_in_icloud.append(uid)
        else:
            # In both — compare content
            g_ev = google_by_uid[uid]
            i_comp = icloud_by_uid[uid]

            g_title = g_ev.get('summary', '').strip()
            i_title = str(i_comp.get('summary', '')).strip()

            g_start, g_end = google_event_times(g_ev)
            i_start, i_end = icloud_event_times(i_comp)

            mismatches = []
            if g_title.lower() != i_title.lower():
                mismatches.append(f"title: Google='{g_title}' iCloud='{i_title}'")
            if g_start != i_start:
                mismatches.append(f"start: Google={g_start} iCloud={i_start}")
            if g_end != i_end:
                mismatches.append(f"end:   Google={g_end} iCloud={i_end}")

            if mismatches:
                in_both_mismatch.append((uid, g_ev, i_comp, mismatches))
            else:
                in_both_match.append(uid)

    # ---------------------------------------------------------------------------
    # Report
    # ---------------------------------------------------------------------------
    logger.info("\n" + "=" * 60)
    logger.info("RESULTS")
    logger.info("=" * 60)

    logger.info(f"\n[OK] Matching in both calendars: {len(in_both_match)}")
    for uid in in_both_match:
        g_ev = google_by_uid[uid]
        logger.debug(f"  ✓ {g_ev.get('summary', 'No Title')!r}  (UID: {uid[:40]}...)")

    logger.info(f"\n[MISMATCH] In both but content differs: {len(in_both_mismatch)}")
    for uid, g_ev, i_comp, mismatches in in_both_mismatch:
        g_title = g_ev.get('summary', 'No Title')
        g_start, g_end = google_event_times(g_ev)
        i_start, i_end = icloud_event_times(i_comp)
        i_title = str(i_comp.get('summary', 'No Title'))
        i_desc = str(i_comp.get('description', '')) or ''
        g_desc = g_ev.get('description', '') or ''
        logger.warning(f"  ~ {g_title!r}  (UID: {uid[:40]}...)")
        for m in mismatches:
            logger.warning(f"      DIFF  {m}")
        g_start_raw = g_ev['start'].get('dateTime') or g_ev['start'].get('date', '')
        g_end_raw = g_ev['end'].get('dateTime') or g_ev['end'].get('date', '')
        i_start_raw = str(i_comp.get('dtstart').dt) if i_comp.get('dtstart') else ''
        i_end_raw = str(i_comp.get('dtend').dt) if i_comp.get('dtend') else ''
        logger.debug(f"      Google  title={g_title!r}  start={g_start}(raw:{g_start_raw})  end={g_end}(raw:{g_end_raw})  desc={g_desc[:80]!r}")
        logger.debug(f"      iCloud  title={i_title!r}  start={i_start}(raw:{i_start_raw})  end={i_end}(raw:{i_end_raw})  desc={i_desc[:80]!r}")
        state_entry = synced_events.get(uid)
        if state_entry:
            logger.debug(f"      sync_state: source={state_entry.get('source')} "
                         f"synced_at={state_entry.get('synced_at')} "
                         f"last_modified={state_entry.get('last_modified')}")
        else:
            logger.debug(f"      sync_state: NOT in state")

    logger.info(f"\n[GOOGLE ONLY] In Google, missing from iCloud: {len(only_in_google)}")
    for uid in only_in_google:
        g_ev = google_by_uid[uid]
        g_start, _ = google_event_times(g_ev)
        state_entry = synced_events.get(uid)
        state_info = (
            f"source={state_entry.get('source')} synced_at={state_entry.get('synced_at')}"
            if state_entry else "NOT in sync state"
        )
        logger.warning(f"  → {g_ev.get('summary', 'No Title')!r}  start={g_start}")
        logger.debug(f"      UID: {uid}")
        logger.debug(f"      sync_state: {state_info}")

    logger.info(f"\n[ICLOUD ONLY] In iCloud, missing from Google: {len(only_in_icloud)}")
    for uid in only_in_icloud:
        i_comp = icloud_by_uid[uid]
        i_start, _ = icloud_event_times(i_comp)
        i_title = str(i_comp.get('summary', 'No Title'))
        state_entry = synced_events.get(uid)
        state_info = (
            f"source={state_entry.get('source')} synced_at={state_entry.get('synced_at')}"
            if state_entry else "NOT in sync state"
        )
        logger.warning(f"  ← {i_title!r}  start={i_start}")
        logger.debug(f"      UID: {uid}")
        logger.debug(f"      sync_state: {state_info}")

    # --- Summary ---
    logger.info("\n" + "=" * 60)
    logger.info("SUMMARY")
    logger.info(f"  Matching:        {len(in_both_match)}")
    logger.info(f"  Mismatched:      {len(in_both_mismatch)}")
    logger.info(f"  Google only:     {len(only_in_google)}")
    logger.info(f"  iCloud only:     {len(only_in_icloud)}")
    logger.info("=" * 60)

    return {
        'only_in_google': only_in_google,
        'only_in_icloud': only_in_icloud,
        'in_both_match': in_both_match,
        'in_both_mismatch': in_both_mismatch,
        'google_by_uid': google_by_uid,
        'icloud_by_uid': icloud_by_uid,
        'synced_events': synced_events,
        'state': state,
    }


def resync_orphans(results):
    """Interactively offer to remove iCloud-only events from sync state so the
    next sync will re-push them to Google, or remove Google-only events from
    sync state so they get re-evaluated.

    'Orphan' here means: present in one calendar but absent from the other,
    AND already recorded in sync state (so the sync won't touch them again).
    Removing the state entry forces the next sync to treat the event as new.
    """
    state = results['state']
    synced_events = results['synced_events']
    google_by_uid = results['google_by_uid']
    icloud_by_uid = results['icloud_by_uid']

    # iCloud-only events that are in sync state — removing the state entry will
    # cause sync_icloud_to_google to push them to Google again.
    icloud_orphans = [
        uid for uid in results['only_in_icloud']
        if uid in synced_events
    ]

    # Google-only events that are in sync state — removing the state entry will
    # cause sync_google_to_icloud to push them to iCloud again.
    google_orphans = [
        uid for uid in results['only_in_google']
        if uid in synced_events
    ]

    candidates = []
    for uid in icloud_orphans:
        entry = synced_events[uid]
        i_comp = icloud_by_uid[uid]
        i_start, _ = icloud_event_times(i_comp)
        candidates.append((uid, entry, 'icloud-only (missing from Google)', i_start))

    for uid in google_orphans:
        entry = synced_events[uid]
        g_ev = google_by_uid[uid]
        g_start, _ = google_event_times(g_ev)
        candidates.append((uid, entry, 'google-only (missing from iCloud)', g_start))

    if not candidates:
        logger.info("No orphans with sync state entries found — nothing to resync.")
        return

    print(f"\nFound {len(candidates)} event(s) in sync state that are missing from one calendar.")
    print("Removing an entry from sync state will cause the next sync to re-push it.\n")

    to_remove = []
    for uid, entry, reason, start in candidates:
        title = entry.get('title', 'No Title')
        source = entry.get('source', '?')
        synced_at = entry.get('synced_at', '?')
        print(f"  {title!r}  start={start}  [{reason}]")
        print(f"    state: source={source}  synced_at={synced_at}")
        print(f"    UID: {uid}")
        answer = input("  Remove from sync state? [y/N] ").strip().lower()
        if answer == 'y':
            to_remove.append(uid)
        print()

    if not to_remove:
        logger.info("No entries removed.")
        return

    for uid in to_remove:
        title = synced_events[uid].get('title', uid)
        del state['synced_events'][uid]
        logger.info(f"Removed from sync state: {title!r} ({uid})")

    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)
    logger.info(f"Saved updated sync state ({len(to_remove)} entr{'y' if len(to_remove) == 1 else 'ies'} removed).")
    logger.info("Run a sync now to re-push these events.")


def run_sync():
    """Trigger an immediate sync by importing and calling CalendarSync."""
    logger.info("Running sync...")
    sys.path.insert(0, '/app')
    try:
        from sync_calendars import CalendarSync
    except ImportError:
        # Running locally — try current directory
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from sync_calendars import CalendarSync

    sync = CalendarSync()
    success = sync.run_sync()
    if success:
        logger.info("Sync completed successfully.")
    else:
        logger.error("Sync failed.")
    return success


def list_fanout():
    """Read-only: list legacy fan-out duplicate events (old per-occurrence copies from
    before recurring-event support) so they can be deleted MANUALLY in Google Calendar.
    Makes no changes."""
    from collections import defaultdict
    if not os.path.exists(CONFIG_FILE):
        logger.error(f"Config file not found: {CONFIG_FILE}")
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        config = json.load(f)
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)
    synced = state.get('synced_events', {})
    fanout_keys = [k for k, v in synced.items()
                   if v.get('legacy_fanout') or str(v.get('title') or '').endswith(' (recurring)')]
    if not fanout_keys:
        print("No legacy fan-out entries found in sync state. Nothing to clean up.")
        return

    now = datetime.now(timezone.utc)
    past = int(os.getenv('SYNC_PAST_DAYS', '1'))
    future = int(os.getenv('SYNC_FUTURE_DAYS', '1825'))
    time_min = (now - timedelta(days=past)).isoformat().replace('+00:00', 'Z')
    time_max = (now + timedelta(days=future)).isoformat().replace('+00:00', 'Z')
    google = fetch_google_events(get_google_service(), config['google_calendar_id'], time_min, time_max)
    g_by_uid = {e.get('iCalUID', e['id']): e for e in google}

    groups = defaultdict(list)
    for k in fanout_keys:
        groups[k.rsplit('_', 1)[0] if '_' in k else k].append(k)

    print(f"\nLegacy fan-out duplicates in sync state: {len(fanout_keys)} entr(y/ies) across "
          f"{len(groups)} series.")
    print("These are old per-occurrence copies from before recurring-event support; they are")
    print("excluded from automatic deletion. Delete the ones still present in Google Calendar")
    print("by hand — the sync state self-heals on the next sync. This report changes nothing.\n")
    total_live = 0
    for base, keys in sorted(groups.items()):
        title = synced[keys[0]].get('title', '?')
        live = [k for k in keys if k in g_by_uid]
        total_live += len(live)
        print(f"  • {title}  — {len(keys)} state entr(y/ies), {len(live)} still in Google")
        for k in live[:10]:
            g = g_by_uid[k]
            start = (g['start'].get('dateTime') or g['start'].get('date', ''))[:16]
            print(f"       delete in Google: {g.get('summary', '?')} ({start})  [id {g['id']}]")
        if len(live) > 10:
            print(f"       ... and {len(live) - 10} more still in Google")
    print(f"\n{total_live} fan-out event(s) still present in Google Calendar. No changes were made.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Calendar reconciliation and sync tool.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python reconcile.py                   # reconcile only
  python reconcile.py --sync            # sync, then reconcile
  python reconcile.py --resync-orphans  # reconcile, then interactively fix orphans
  python reconcile.py --sync --resync-orphans  # sync, reconcile, fix orphans
        """,
    )
    parser.add_argument('--sync', action='store_true',
                        help='Run a sync before reconciling')
    parser.add_argument('--resync-orphans', action='store_true',
                        help='Interactively remove orphaned sync state entries so the next sync re-pushes them')
    parser.add_argument('--list-fanout', action='store_true',
                        help='Read-only: list legacy fan-out duplicate events to delete manually')
    args = parser.parse_args()

    if args.list_fanout:
        list_fanout()
        sys.exit(0)

    if args.sync:
        ok = run_sync()
        if not ok:
            sys.exit(1)
        print()

    results = reconcile()

    if args.resync_orphans:
        print()
        resync_orphans(results)
