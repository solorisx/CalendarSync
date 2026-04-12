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
CONFIG_FILE = '/app/data/config.json'
TOKEN_FILE = '/app/data/token.pickle'
STATE_FILE = '/app/data/sync_state.json'
CREDENTIALS_FILE = '/app/data/credentials.json'

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
            singleEvents=True,
            orderBy='startTime',
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
        raw_events = icloud_calendar.date_search(start=start, end=end, expand=True)
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

            uid = str(component.get('uid', ''))
            recurrence_id = component.get('recurrence-id')
            if recurrence_id:
                rec_str = (
                    recurrence_id.dt.isoformat()
                    if hasattr(recurrence_id.dt, 'isoformat')
                    else str(recurrence_id.dt)
                )
                event_id = f"{uid}_{rec_str}"
            else:
                event_id = uid

            parsed.append((event_id, component))

    logger.debug(f"Parsed {len(parsed)} iCloud VEVENT components ({parse_errors} parse errors)")
    return parsed


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def normalise_dt(dt_val):
    """Return an ISO string for comparison, stripping timezone for date-only values."""
    if dt_val is None:
        return None
    if isinstance(dt_val, datetime):
        # Normalise to UTC for comparison
        if dt_val.tzinfo is not None:
            dt_val = dt_val.astimezone(timezone.utc)
        return dt_val.strftime('%Y-%m-%dT%H:%M:%S')
    # date object
    return dt_val.isoformat()


def google_event_times(g_event):
    """Return (start_str, end_str) from a Google event dict."""
    start_raw = g_event['start'].get('dateTime', g_event['start'].get('date', ''))
    end_raw = g_event['end'].get('dateTime', g_event['end'].get('date', ''))
    # Strip timezone for comparison (Google uses offset strings like +02:00)
    try:
        start = normalise_dt(datetime.fromisoformat(start_raw.replace('Z', '+00:00')))
    except Exception:
        start = start_raw
    try:
        end = normalise_dt(datetime.fromisoformat(end_raw.replace('Z', '+00:00')))
    except Exception:
        end = end_raw
    return start, end


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

    # Time window (same as sync: yesterday → +90 days)
    now = datetime.now(timezone.utc)
    start_dt = now - timedelta(days=1)
    end_dt = now + timedelta(days=90)
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

    # Index by iCalUID (primary) and event ID (fallback)
    google_by_uid = {}
    for ev in google_events_list:
        ical_uid = ev.get('iCalUID', ev['id'])
        google_by_uid[ical_uid] = ev
        stripped = ical_uid.lstrip('_')
        if stripped != ical_uid:
            google_by_uid[stripped] = ev
    logger.info(f"Google: {len(google_events_list)} events ({len(google_by_uid)} unique UIDs)")

    # --- Fetch iCloud events ---
    logger.info("Fetching iCloud Calendar events...")
    icloud_calendar = get_icloud_calendar(config)
    icloud_events_list = fetch_icloud_events(icloud_calendar, start_dt, end_dt)

    icloud_by_uid = {}
    for event_id, component in icloud_events_list:
        icloud_by_uid[event_id] = component
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
        g_start, _ = google_event_times(g_ev)
        logger.warning(f"  ~ {g_title!r}  start={g_start}  (UID: {uid[:40]}...)")
        for m in mismatches:
            logger.warning(f"      {m}")
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

    # --- Sync state orphans (in state but not in either calendar within window) ---
    state_orphans = []
    for uid, entry in synced_events.items():
        if uid not in google_by_uid and uid not in icloud_by_uid:
            state_orphans.append((uid, entry))

    logger.info(f"\n[STATE ORPHANS] In sync state but absent from both calendars (within window): {len(state_orphans)}")
    for uid, entry in state_orphans:
        logger.debug(f"  ? {entry.get('title', 'No Title')!r}  "
                     f"start={entry.get('start')}  source={entry.get('source')}  "
                     f"synced_at={entry.get('synced_at')}")

    # --- Summary ---
    logger.info("\n" + "=" * 60)
    logger.info("SUMMARY")
    logger.info(f"  Matching:        {len(in_both_match)}")
    logger.info(f"  Mismatched:      {len(in_both_mismatch)}")
    logger.info(f"  Google only:     {len(only_in_google)}")
    logger.info(f"  iCloud only:     {len(only_in_icloud)}")
    logger.info(f"  State orphans:   {len(state_orphans)}")
    logger.info("=" * 60)


if __name__ == '__main__':
    reconcile()
