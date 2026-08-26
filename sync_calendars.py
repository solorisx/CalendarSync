#!/usr/bin/env python3
"""
Bidirectional Calendar Sync: Google Calendar <-> iCloud
With ntfy.sh notifications
"""

import os
import sys
import json
import pickle
import requests
import time
import logging
import hashlib
import re
from io import StringIO
from datetime import datetime, timedelta, timezone, date
from icalendar import Calendar, Event, Alarm
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import caldav

# Context manager to suppress caldav error output
class SuppressCaldavOutput:
    def __enter__(self):
        self.old_stderr = sys.stderr
        sys.stderr = StringIO()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        sys.stderr = self.old_stderr
        return False

# Configuration
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

# UIDs we mint for one-way mirrored events: "ow-<12-hex calendar hash>-<id>".
# Structural, so a mirrored event can be recognised without consulting sync state.
ONEWAY_UID_RE = re.compile(r'^ow-[0-9a-f]{12}-')

SCOPES = ['https://www.googleapis.com/auth/calendar']
SYNC_INTERVAL = int(os.getenv('SYNC_INTERVAL', '900'))  # 15 minutes default
HEARTBEAT_INTERVAL = int(os.getenv('HEARTBEAT_INTERVAL', '172800'))  # 48 hours default
HEARTBEAT_DAY_START = int(os.getenv('HEARTBEAT_DAY_START', '8'))   # hour (0-23), inclusive
HEARTBEAT_DAY_END = int(os.getenv('HEARTBEAT_DAY_END', '22'))       # hour (0-23), exclusive
RECONCILE_HOUR = int(os.getenv('RECONCILE_HOUR', '14'))  # hour of day to run daily reconcile
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()  # DEBUG, INFO, WARNING, ERROR

# Sync time window. The PAST bound keeps recently-passed events in scope for a short
# while; the FUTURE bound is intentionally large. The window's only historical job was
# to bound infinite recurring-event expansion — once recurring events are synced as
# real recurring series (RRULE), the future bound must NOT clip ordinary future events,
# so it defaults to ~5 years. Both are overridable via env or config.json.
SYNC_PAST_DAYS = int(os.getenv('SYNC_PAST_DAYS', '1'))
SYNC_FUTURE_DAYS = int(os.getenv('SYNC_FUTURE_DAYS', '1825'))

log_format = '%(asctime)s - %(levelname)s - %(message)s'
for handler in logging.root.handlers:
    handler.setFormatter(logging.Formatter(log_format, datefmt='%Y-%m-%d %H:%M:%S'))
    handler.setLevel(getattr(logging, LOG_LEVEL))
logging.root.setLevel(getattr(logging, LOG_LEVEL))

logger = logging.getLogger(__name__)
logger.propagate = False  # Prevent duplicate logs

# Add handler to logger explicitly
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter(log_format, datefmt='%Y-%m-%d %H:%M:%S'))
logger.addHandler(handler)
logger.setLevel(getattr(logging, LOG_LEVEL))

# Configure third-party library logging
if LOG_LEVEL == 'DEBUG':
    # Enable detailed HTTP logging for caldav and Google API
    logging.getLogger('caldav').setLevel(logging.DEBUG)
    logging.getLogger('urllib3').setLevel(logging.INFO)  # Too verbose at DEBUG
    logging.getLogger('googleapiclient').setLevel(logging.INFO)
else:
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('googleapiclient').setLevel(logging.WARNING)

def _is_permanent_http_error(exc):
    """True for HTTP failures that retrying can never fix: a read-only calendar
    (403), a missing event (404/410), a rejected body (400)."""
    status = getattr(getattr(exc, 'resp', None), 'status', None)
    return status in (400, 401, 403, 404, 410)


def retry(fn, retries=3, delay=5, backoff=2, give_up=None):
    """Call fn(), retrying on exception with exponential backoff.

    give_up(exc) -> True marks an error as permanent and re-raises it at once
    instead of sleeping through retries that cannot succeed."""
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            if attempt == retries - 1 or (give_up is not None and give_up(e)):
                raise
            wait = delay * (backoff ** attempt)
            logger.warning(f"Request failed ({e}), retrying in {wait}s (attempt {attempt + 1}/{retries})...")
            time.sleep(wait)


def _parse_instant(value):
    """Parse a timestamp (RFC3339 / ISO 8601, with 'Z' or offset, date or
    datetime) into a timezone-aware UTC datetime. Returns None if unparseable."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _google_event_end(event):
    """A Google event's end as an ISO string, falling back to its start.

    Used to age state entries out on when the event actually finishes. Google
    always returns `end`, but a missing one must not crash the sync — falling
    back to the start just restores the older start-based behaviour."""
    end = event.get('end') or {}
    start = event.get('start') or {}
    return (end.get('dateTime') or end.get('date')
            or start.get('dateTime') or start.get('date'))


def _icloud_component_end(component, fallback):
    """A VEVENT's end as an ISO string, falling back to `fallback` (its start).

    DTEND is optional in iCalendar — a VEVENT may carry DURATION instead, or
    neither — so both forms are handled and neither is assumed."""
    prop = component.get('dtend')
    if prop is not None:
        dt = prop.dt
        return dt.isoformat() if isinstance(dt, (datetime, date)) else str(dt)
    dur = component.get('duration')
    start_prop = component.get('dtstart')
    if dur is not None and start_prop is not None and isinstance(dur.dt, timedelta):
        dt = start_prop.dt + dur.dt
        return dt.isoformat() if isinstance(dt, (datetime, date)) else str(dt)
    return fallback


def _same_instant(a, b):
    """Compare two timestamps as points in time, tolerant of format/precision
    differences (e.g. Google's millisecond 'Z' form vs iCloud's second-precision
    offset form). Falls back to raw equality only if neither parses.

    NOTE: change detection compares each source's timestamp against its OWN
    stored baseline. Never compare a Google timestamp against an iCloud one:
    they use different formats and precisions and will never match, which is
    exactly what produced the update ping-pong."""
    da, db = _parse_instant(a), _parse_instant(b)
    if da is None or db is None:
        return a == b
    return abs((da - db).total_seconds()) < 1.0

class CalendarSync:
    def __init__(self):
        self.config = self.load_config()
        self.state = self.load_state()

    def load_config(self):
        """Load configuration from file"""
        if not os.path.exists(CONFIG_FILE):
            logger.error(f"{CONFIG_FILE} not found. Please create it.")
            sys.exit(1)
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)

    def _sync_window(self, now=None):
        """Return the sync time window as (now, start_dt, end_dt, time_min, time_max).

        Past/future bounds are configurable via env (SYNC_PAST_DAYS / SYNC_FUTURE_DAYS)
        or config.json (sync_past_days / sync_future_days), config taking precedence.
        This is the single source of truth for the query window — every Google
        events().list and CalDAV date_search, and the deletion-detection gates, derive
        their bounds from here so they can never drift apart again."""
        if now is None:
            now = datetime.now(timezone.utc)
        past = int(self.config.get('sync_past_days', SYNC_PAST_DAYS))
        future = int(self.config.get('sync_future_days', SYNC_FUTURE_DAYS))
        start_dt = now - timedelta(days=past)
        end_dt = now + timedelta(days=future)
        time_min = start_dt.isoformat().replace('+00:00', 'Z')
        time_max = end_dt.isoformat().replace('+00:00', 'Z')
        return now, start_dt, end_dt, time_min, time_max

    def _target_updates_enabled(self, direction):
        """Is write-back of edits made on the TARGET side enabled?

        direction is 'to_google' (a Google-origin event edited in iCloud is patched
        back into Google) or 'to_icloud' (an iCloud-origin event edited in Google is
        written back into the iCloud event). Both default on; a bare bool is accepted
        as shorthand for the whole block. One-way mirrored calendars are governed
        separately by a per-calendar 'writeback' key, since most of them are
        read-only shares."""
        cfg = self.config.get('sync_target_updates', True)
        if isinstance(cfg, bool):
            return cfg
        return bool((cfg or {}).get(direction, True))

    @staticmethod
    def _refresh_entry_times(entry, start, end, title):
        """Keep a state entry's cached start/end/title in step with a write.

        Update paths used to leave the ORIGINAL times in state. That is not cosmetic:
        _cleanup_past_events and both deletion-window gates read these fields, so a
        rescheduled event could be pruned as 'past' and then re-created as a duplicate.
        Write-back makes reschedules routine, so every update path refreshes them."""
        if start:
            entry['start'] = start
        if end:
            entry['end'] = end
        if title is not None:
            entry['title'] = title

    @classmethod
    def _apply_google_fields_to_vevent(cls, component, event, summary=None, recurrence=None):
        """Overwrite a VEVENT's synced fields from a Google event, in place.

        Only the fields this app syncs are touched — everything else the VEVENT
        carries (VALARM, attendees, custom properties) is deliberately preserved.
        Pass recurrence=None to leave RRULE/RDATE/EXDATE alone (the one-way mirror
        writes expanded single events and must not grow a recurrence)."""
        component.pop('summary', None)
        component.add('summary', summary if summary is not None else event.get('summary', 'No Title'))
        if event.get('description'):
            component.pop('description', None)
            component.add('description', event['description'])
        elif 'description' in component:
            del component['description']
        ev_start, ev_end = cls._google_start_end(event)
        component.pop('dtstart', None)
        component.pop('dtend', None)
        component.add('dtstart', ev_start)
        component.add('dtend', ev_end)
        if recurrence is not None:
            component.pop('rrule', None)
            component.pop('rdate', None)
            component.pop('exdate', None)
            cls._apply_google_recurrence(component, recurrence)

    @staticmethod
    def _google_start_end(event):
        """Return (dtstart, dtend) for a Google event as icalendar-ready values:
        a UTC ``datetime`` for timed events, or a ``date`` for all-day events.

        Google represents all-day events with a ``date`` key (no ``dateTime``) and an
        *exclusive* end date — which is exactly iCal's VALUE=DATE DTEND convention, so a
        ``date`` object round-trips as an all-day VEVENT. The previous code fell back to
        the ``date`` string and ran it through ``datetime.fromisoformat``, turning all-day
        events into midnight-UTC *timed* events (and shifting them by timezone)."""
        s, e = event['start'], event['end']
        if 'dateTime' not in s and 'date' in s:
            return date.fromisoformat(s['date']), date.fromisoformat(e['date'])
        start_dt = datetime.fromisoformat(s['dateTime'].replace('Z', '+00:00'))
        end_dt = datetime.fromisoformat(e['dateTime'].replace('Z', '+00:00'))
        if start_dt.tzinfo is not None:
            start_dt = start_dt.astimezone(timezone.utc)
            end_dt = end_dt.astimezone(timezone.utc)
        return start_dt, end_dt

    @staticmethod
    def _apply_google_recurrence(component, recurrence_list):
        """Copy Google's ``recurrence[]`` content-lines (RRULE/RDATE/EXDATE, already valid
        ICS property lines) onto an icalendar VEVENT, preserving params like TZID by
        round-tripping them through the parser. This is how a recurring Google master is
        written to iCloud as a real recurring VEVENT instead of expanded copies."""
        if not recurrence_list:
            return
        stub = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:stub\r\n"
                + "\r\n".join(recurrence_list) + "\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        parsed = Calendar.from_ical(stub)
        for sub in parsed.walk('VEVENT'):
            for k in ('RRULE', 'RDATE', 'EXDATE'):
                if k in sub:
                    component.pop(k, None)
                    component.add(k, sub[k])

    @staticmethod
    def _extract_ical_recurrence(component):
        """Serialize a VEVENT's rrule/rdate/exdate back into Google ``recurrence[]`` strings
        (with TZID params preserved). This is how a recurring iCloud master is written to
        Google as a real series."""
        out = []
        for k in ('RRULE', 'RDATE', 'EXDATE'):
            if k not in component:
                continue
            props = component[k]
            if not isinstance(props, list):
                props = [props]
            for p in props:
                val = p.to_ical()
                if isinstance(val, (bytes, bytearray)):
                    val = val.decode('utf-8')
                tzid = getattr(p, 'params', {}).get('TZID')
                out.append(f"{k}{';TZID=' + str(tzid) if tzid else ''}:{val}")
        return out

    @staticmethod
    def _google_exdate_line(raw):
        """Build an EXDATE content line from a Google originalStartTime value:
        a datetime ('2026-03-02T09:00:00Z' or with offset) or an all-day date
        ('2026-03-02'). Used to translate a cancelled Google occurrence into an
        EXDATE on the iCloud master."""
        if 'T' in raw:
            dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc)
            return "EXDATE:" + dt.strftime("%Y%m%dT%H%M%SZ")
        return "EXDATE;VALUE=DATE:" + raw.replace('-', '')

    def load_state(self):
        """Load sync state"""
        default = {'last_sync': None, 'synced_events': {}, 'last_error': None, 'last_notification_sent': None}
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r') as f:
                    loaded = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                # A truncated/corrupt state file must not crash the service loop.
                # Fall back to a backup if one exists, otherwise start clean —
                # the next sync rebuilds state from both calendars.
                logger.error(f"Sync state file is unreadable ({e}); attempting recovery")
                backup = STATE_FILE + '.bak'
                if os.path.exists(backup):
                    try:
                        with open(backup, 'r') as f:
                            loaded = json.load(f)
                        logger.warning("Recovered sync state from backup")
                    except (json.JSONDecodeError, OSError):
                        logger.error("Backup also unreadable; starting with empty state")
                        loaded = dict(default)
                else:
                    loaded = dict(default)
            # Ensure all expected keys are present (older state files may lack some)
            for k, v in default.items():
                loaded.setdefault(k, v)
            self._migrate_fanout(loaded)
            return loaded
        return dict(default)

    @staticmethod
    def _migrate_fanout(loaded):
        """Neutralize legacy per-occurrence 'fan-out' entries from the old expand-everything
        sync. Those entries carry the historical ' (recurring)' title suffix. We KEEP them so
        the leftover Google singles stay recognized as already-synced (the is_icloud_origin
        guard then leaves them alone, so they are never re-pushed to iCloud), but flag them so
        deletion-detection never auto-removes them — cutover cleanup of those duplicates is
        manual (see reconcile.py --list-fanout). Idempotent."""
        migrated = 0
        for entry in loaded.get('synced_events', {}).values():
            if str(entry.get('title') or '').endswith(' (recurring)') and not entry.get('legacy_fanout'):
                entry['legacy_fanout'] = True
                migrated += 1
        if migrated:
            logger.info(f"Migration: flagged {migrated} legacy fan-out event(s); excluded from "
                        f"deletion. Run 'reconcile.py --list-fanout' to clean up the duplicates.")

    def save_state(self):
        """Save sync state atomically.

        Writing in place risks a truncated, unparseable file if the process is
        killed mid-write (which would then crash the next startup). Write to a
        temp file, keep the previous good copy as a backup, then atomically
        replace."""
        tmp = STATE_FILE + '.tmp'
        try:
            with open(tmp, 'w') as f:
                json.dump(self.state, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            if os.path.exists(STATE_FILE):
                try:
                    os.replace(STATE_FILE, STATE_FILE + '.bak')
                except OSError:
                    pass
            os.replace(tmp, STATE_FILE)
        except OSError as e:
            logger.error(f"Failed to save sync state: {e}")

    def _cleanup_past_events(self):
        """Remove past events from sync state to keep it lean.

        The cutoff is kept a full day BEHIND the sync window's trailing edge
        (now - 1 day). If cleanup dropped events still inside the active window,
        the next sync would re-record them from scratch — resetting their change
        detection baseline and re-igniting the update ping-pong — or the deletion
        pass could momentarily treat them as user-initiated deletions. The extra
        day of margin, combined with aging out on the event's end, guarantees an
        event has fully left the sync window before it becomes eligible for
        cleanup. Datetime comparison (not date-only) is
        used so it stays consistent with the datetime-based sync window."""
        past = int(self.config.get('sync_past_days', SYNC_PAST_DAYS))
        cutoff = datetime.now(timezone.utc) - timedelta(days=past + 1)
        to_remove = []
        for event_id, event_info in self.state['synced_events'].items():
            # Age out on the event's END, not its start. A long event (say Aug 24 ->
            # Sep 4) has a start that falls behind the window's trailing edge while
            # the event is still live: Google's timeMin matches on end time, so it
            # keeps being returned. Pruning on start would drop the entry out from
            # under an event still flowing through the sync — and that entry is what
            # marks it already-synced, so the next pass re-creates it on the other
            # side as a duplicate. Falls back to start for pre-'end' entries.
            event_bound = event_info.get('end') or event_info.get('start')
            if not event_bound:
                continue
            dt = _parse_instant(event_bound)
            if dt is None:
                continue
            if dt < cutoff:
                to_remove.append(event_id)
        for event_id in to_remove:
            del self.state['synced_events'][event_id]
        if to_remove:
            logger.debug(f"Removed {len(to_remove)} past event(s) from sync state")

    def _record_synced_event(self, event_id, title, source, start, last_modified=None, failed=False, error=None, icloud_uid=None, last_modified_google=None, last_modified_icloud=None, source_calendar_id=None, end=None):
        """Record a synced event in the state"""
        existing = self.state['synced_events'].get(event_id, {})
        entry = {
            'title': title,
            'synced_at': datetime.now().isoformat(),
            'source': source,
            'start': start,
            # The event's END bounds how long it stays inside the sync window, so it
            # is what cleanup must age out on. Preserved across re-records; absent on
            # entries written by older versions (cleanup falls back to start there).
            'end': end or existing.get('end'),
            'last_modified': last_modified,
            # Store per-source timestamps to avoid update ping-pong.
            # Each sync direction compares against its own source timestamp only.
            'last_modified_google': last_modified_google or existing.get('last_modified_google'),
            'last_modified_icloud': last_modified_icloud or existing.get('last_modified_icloud'),
        }
        # For one-way mirrored calendars, remember which Google calendar the event
        # came from so deletion detection can be scoped per source calendar.
        source_calendar_id = source_calendar_id or existing.get('source_calendar_id')
        if source_calendar_id:
            entry['source_calendar_id'] = source_calendar_id
        if failed:
            entry['sync_failed'] = True
            entry['error'] = error
        if icloud_uid and icloud_uid != event_id:
            # Track the UID actually written to iCloud (may be hashed) so the
            # iCloud→Google direction can recognise the event and skip it.
            entry['icloud_uid'] = icloud_uid
        elif existing.get('icloud_uid'):
            entry['icloud_uid'] = existing['icloud_uid']
        self.state['synced_events'][event_id] = entry

    def cleanup_google_orphans(self, dry_run=True):
        """Delete events from Google Calendar that are not in sync state.

        Args:
            dry_run: If True, only show what would be deleted without actually deleting
        """
        google_service = self.get_google_service()

        now, start_dt, end_dt, time_min, time_max = self._sync_window()

        # Get all Google events
        events_result = google_service.events().list(
            calendarId=self.config['google_calendar_id'],
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy='startTime'
        ).execute()

        events = events_result.get('items', [])
        logger.info(f"Found {len(events)} events in Google Calendar")

        # Get synced event IDs (both as event ID and iCalUID)
        synced_ids = set(self.state['synced_events'].keys())

        orphans = []
        for event in events:
            event_id = event['id']
            ical_uid = event.get('iCalUID', '')

            # Check if event is in sync state (by either ID or iCalUID)
            if event_id not in synced_ids and ical_uid not in synced_ids:
                orphans.append(event)

        if not orphans:
            logger.info("No orphan events found")
            return

        logger.info(f"Found {len(orphans)} orphan event(s) not in sync state:")
        for event in orphans:
            start = event['start'].get('dateTime', event['start'].get('date'))
            logger.info(f"  - {event.get('summary', 'No Title')} ({start})")

        if dry_run:
            logger.info("Dry run - no events deleted. Call with dry_run=False to delete.")
            return

        deleted_count = 0
        for event in orphans:
            try:
                google_service.events().delete(
                    calendarId=self.config['google_calendar_id'],
                    eventId=event['id']
                ).execute()
                deleted_count += 1
                logger.info(f"Deleted: {event.get('summary', 'No Title')}")
            except Exception as e:
                logger.error(f"Failed to delete {event.get('summary')}: {e}")

        logger.info(f"Deleted {deleted_count} orphan event(s) from Google Calendar")

    def send_notification(self, title, message):
        """Send notification to ntfy.sh"""
        notify_url = self.config.get('notify_url')
        if not notify_url:
            logger.info(f"Notification: {title} - {message}")
            self.state['last_notification_sent'] = datetime.now().isoformat()
            return

        try:
            response = requests.post(notify_url, data=message.encode('utf-8'))
            if response.status_code == 200:
                logger.info(f"Notification sent: {title}")
                self.state['last_notification_sent'] = datetime.now().isoformat()
            else:
                logger.warning(f"Failed to send notification (HTTP {response.status_code}): {title}")
        except Exception as e:
            logger.error(f"Error sending notification: {e}")

    def get_google_service(self):
        """Authenticate and return Google Calendar service"""
        creds = None

        if os.path.exists(TOKEN_FILE):
            with open(TOKEN_FILE, 'rb') as token:
                creds = pickle.load(token)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                logger.debug("Refreshing expired Google OAuth token")
                creds.refresh(Request())
            else:
                if not os.path.exists(CREDENTIALS_FILE):
                    logger.error(f"{CREDENTIALS_FILE} not found")
                    logger.error("Download from Google Cloud Console")
                    sys.exit(1)
                logger.info("Starting Google OAuth authentication flow")
                flow = InstalledAppFlow.from_client_secrets_file(
                    CREDENTIALS_FILE, SCOPES)
                # Use port 8080 for Docker compatibility
                creds = flow.run_local_server(port=8080, host='0.0.0.0')

            with open(TOKEN_FILE, 'wb') as token:
                pickle.dump(creds, token)
            logger.debug("Google OAuth token saved")

        return build('calendar', 'v3', credentials=creds)

    def get_icloud_calendar(self):
        """Connect to iCloud calendar"""
        icloud_config = self.config['icloud']

        client = caldav.DAVClient(
            url=icloud_config['url'],
            username=icloud_config['username'],
            password=icloud_config['password']
        )

        principal = client.principal()
        calendars = principal.calendars()

        if not calendars:
            raise Exception("No iCloud calendars found")

        # Return specified calendar or first one
        calendar_name = icloud_config.get('calendar_name')
        if calendar_name:
            for cal in calendars:
                if cal.name == calendar_name:
                    return cal

        raise Exception(f"iCloud calendar '{calendar_name}' not found, available: {[cal.name for cal in calendars]}")

    @staticmethod
    def _sanitize_icloud_uid(uid):
        """Coerce a UID into a form iCloud accepts.

        iCloud rejects UIDs starting with '_' (Google-internal format) and UIDs
        longer than ~255 chars, so strip the prefix and hash overly long ones."""
        safe = str(uid).lstrip('_')
        if len(safe) > 200:
            safe = hashlib.sha256(safe.encode()).hexdigest()
            logger.debug(f"UID too long, hashed to: {safe}")
        return safe

    def _icloud_uid_for_google_event(self, event, existing_icloud_events):
        """Pick the iCloud UID to use for a Google event.

        Google's event `id` is not a cross-calendar identity. An event that came
        FROM iCloud keeps its original UID in `iCalUID` while Google assigns an
        unrelated `id`; deriving the iCloud UID from that id then fails to
        recognise the very event we pushed there ourselves, and writes a second
        copy back into iCloud — the ping-pong duplicate. So prefer the iCalUID
        whenever it is foreign, i.e. not Google's own "<id>@google.com" form.

        Changing the derivation must not orphan events that existing installs
        already wrote under the id-derived spelling, so anything we have a
        recorded UID for, or that is actually present in iCloud under the old
        spelling, keeps it."""
        legacy_uid = self._sanitize_icloud_uid(event['id'])

        # Whatever we last wrote for this event is the authoritative answer.
        stored = self.state['synced_events'].get(event['id'], {}).get('icloud_uid')
        if stored:
            return stored
        # Already in iCloud under the legacy spelling → keep using it.
        if legacy_uid in existing_icloud_events:
            return legacy_uid

        ical_uid = str(event.get('iCalUID') or '')
        if ical_uid and not ical_uid.endswith('@google.com'):
            foreign_uid = self._sanitize_icloud_uid(ical_uid)
            if foreign_uid != legacy_uid:
                return foreign_uid
        return legacy_uid

    def sync_google_to_icloud(self, google_service, icloud_calendar):

        """Sync events from Google Calendar to iCloud"""
        logger.info("→ Syncing Google → iCloud...")

        now, start_dt, end_dt, time_min, time_max = self._sync_window()

        logger.debug(f"Querying Google Calendar: {self.config['google_calendar_id']}")
        logger.debug(f"Time range: {time_min} to {time_max}")

        events = []
        page_token = None
        while True:
            events_result = google_service.events().list(
                calendarId=self.config['google_calendar_id'],
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=False,
                showDeleted=True,  # surface cancelled occurrences -> EXDATE on the master
                pageToken=page_token
            ).execute()
            events.extend(events_result.get('items', []))
            page_token = events_result.get('nextPageToken')
            if not page_token:
                break

        logger.info(f"Fetched {len(events)} events from Google Calendar")
        logger.debug(f"Number of events in 'items': {len(events)}")
        synced_count = 0
        updated_count = 0
        deleted_count = 0
        error_count = 0
        added_events = []
        updated_events = []
        deleted_events = []

        # With singleEvents=False a recurring series returns as ONE master event carrying
        # a recurrence[] array (RRULE/RDATE/EXDATE) — written to iCloud as a single recurring
        # VEVENT rather than N per-occurrence copies. Exception items (recurringEventId set)
        # are individual modified/cancelled occurrences:
        #   - cancelled occurrence  -> add its originalStartTime as an EXDATE on the master
        #   - modified occurrence   -> not synced as a separate event in this pass
        # cancelled_exdates maps a master's Google id -> list of EXDATE content lines.
        cancelled_exdates = {}
        base_events = []
        for event in events:
            parent = event.get('recurringEventId')
            if event.get('status') == 'cancelled':
                if parent:
                    ost = event.get('originalStartTime') or {}
                    raw = ost.get('dateTime') or ost.get('date')
                    if raw:
                        cancelled_exdates.setdefault(parent, []).append(self._google_exdate_line(raw))
                # a cancelled single/master (not an exception) is just gone — ignore here;
                # deletion propagation handles removing it from iCloud.
                continue
            if parent:
                logger.debug(f"  Skipping modified recurring override (not synced separately): {event.get('summary')}")
                continue
            base_events.append(event)
        events = base_events

        def _recurrence_for(ev):
            """Master recurrence[] merged with any cancelled-occurrence EXDATEs."""
            return list(ev.get('recurrence') or []) + cancelled_exdates.get(ev['id'], [])

        # Track current Google event UIDs (use plain event ID, not iCalUID)
        # We use event['id'] to match what we store in sync_state
        current_google_ids = {event['id'] for event in events}
        logger.debug(f"Current Google event IDs: {len(current_google_ids)} unique events")

        # Get existing iCloud events to check for duplicates
        existing_icloud_events = {}
        try:
            with SuppressCaldavOutput():
                icloud_events = icloud_calendar.date_search(
                    start=start_dt,
                    end=end_dt,
                    expand=False
                )
            for icloud_event in icloud_events:
                try:
                    ical = Calendar.from_ical(icloud_event.data)
                    for component in ical.walk():
                        if component.name == "VEVENT":
                            # expand=False → one VEVENT per event (masters carry RRULE). Skip
                            # RECURRENCE-ID override instances; index masters/singles by UID.
                            if component.get('recurrence-id'):
                                continue
                            existing_icloud_events[str(component.get('uid'))] = icloud_event
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Could not fetch existing iCloud events: {e}")

        # Add new events
        logger.debug(f"Processing {len(events)} Google events...")
        for event in events:
            event_uid = event['id']
            event_title = event.get('summary', 'No Title')
            safe_uid = self._icloud_uid_for_google_event(event, existing_icloud_events)
            logger.debug(f"Checking event: {event_title} (ID: {event_uid}, iCalUID: {event.get('iCalUID')}, iCloud UID: {safe_uid})")

            # Check if event originated from iCloud by looking it up in sync state.
            # Using iCalUID-based heuristics is unreliable: ICS-imported events also have
            # a foreign iCalUID that doesn't match Google's event ID, causing false positives.
            ical_uid = event.get('iCalUID', '')
            is_icloud_origin = (
                ical_uid and self.state['synced_events'].get(ical_uid, {}).get('source') == 'icloud'
            )
            if is_icloud_origin:
                # iCloud owns this event's content. When write-back is enabled, an edit
                # made HERE (on the Google copy) is still propagated home to iCloud —
                # otherwise it is silently dropped, which is the old known limitation.
                if not self._target_updates_enabled('to_icloud'):
                    logger.debug(f"  Skipping: Event originated from iCloud (write-back to iCloud disabled)")
                    continue
                entry = self.state['synced_events'][ical_uid]
                if entry.get('sync_failed') or entry.get('legacy_fanout'):
                    continue
                last_modified = event.get('updated')
                baseline = entry.get('last_modified_google')
                if baseline is None:
                    # No Google-side baseline yet for this iCloud-origin event (every
                    # entry written before this feature). Capture and do not propagate.
                    entry['last_modified_google'] = last_modified
                    logger.debug(f"  Captured Google baseline for iCloud-origin '{event_title}' (no propagation)")
                    continue
                if not last_modified or _same_instant(last_modified, baseline):
                    logger.debug(f"  Skipping: iCloud-origin event unchanged in Google")
                    continue
                # The state key IS the iCloud UID for iCloud-origin events.
                icloud_event = (
                    existing_icloud_events.get(ical_uid)
                    or existing_icloud_events.get(self._sanitize_icloud_uid(ical_uid))
                )
                if icloud_event is None:
                    logger.warning(f"  Google edit to '{event_title}' not written back: event not found in iCloud window")
                    continue
                try:
                    cal = Calendar.from_ical(icloud_event.data)
                    for component in cal.walk():
                        if component.name == "VEVENT":
                            self._apply_google_fields_to_vevent(
                                component, event, recurrence=_recurrence_for(event)
                            )
                            break
                    icloud_event.data = cal.to_ical()
                    icloud_event.save()
                    entry['last_modified_google'] = last_modified
                    entry['last_modified'] = last_modified
                    # Our write may bump iCloud's LAST-MODIFIED; invalidate that baseline
                    # so the reverse pass re-captures instead of bouncing it back.
                    entry['last_modified_icloud'] = None
                    entry.pop('icloud_lm_absent', None)
                    event_start = event['start'].get('dateTime', event['start'].get('date'))
                    self._refresh_entry_times(
                        entry, event_start, _google_event_end(event), event.get('summary')
                    )
                    updated_count += 1
                    updated_events.append({'title': event.get('summary'), 'start': event_start})
                    logger.info(f"Written back to iCloud: {event_title}")
                except Exception as e:
                    logger.error(f"Failed to write '{event_title}' back to iCloud: {e}")
                    error_count += 1
                continue

            if event_uid in self.state['synced_events'] and not self.state['synced_events'][event_uid].get('sync_failed'):
                # Event already successfully synced — propagate only if Google's
                # OWN timestamp advanced past the stored Google baseline. Never
                # fall back to the iCloud timestamp: that cross-source comparison
                # is what produced the update ping-pong.
                last_modified = event.get('updated')
                stored_state = self.state['synced_events'][event_uid]
                baseline = stored_state.get('last_modified_google')
                if baseline is None:
                    # No Google baseline yet (e.g. a legacy state entry). Capture
                    # it now and do not propagate on this pass.
                    stored_state['last_modified_google'] = last_modified
                    logger.debug(f"  Captured Google baseline for '{event_title}' (no propagation)")
                    continue
                logger.debug(f"  Event already synced. Checking for modifications...")
                logger.debug(f"    Last modified in Google: {last_modified}")
                logger.debug(f"    Stored Google baseline: {baseline}")
                # Also propagate when the recurrence changed (e.g. an occurrence was
                # cancelled) — that can alter the series without bumping the master's
                # `updated` timestamp, so a timestamp-only check would miss it.
                cur_rec_sig = sorted(_recurrence_for(event))
                stored_rec_sig = stored_state.get('recurrence_sig')
                recurrence_changed = stored_rec_sig is not None and cur_rec_sig != stored_rec_sig
                if (last_modified and not _same_instant(last_modified, baseline)) or recurrence_changed:
                    logger.debug(f"  Event modified: {event_title} (was: {baseline}, now: {last_modified}, recurrence_changed={recurrence_changed})")
                    # Update the existing iCloud event
                    if safe_uid in existing_icloud_events:
                        try:
                            icloud_event = existing_icloud_events[safe_uid]
                            cal = Calendar.from_ical(icloud_event.data)
                            for component in cal.walk():
                                if component.name == "VEVENT":
                                    # Refresh recurrence too (RRULE/EXDATE may have changed).
                                    self._apply_google_fields_to_vevent(
                                        component, event, recurrence=_recurrence_for(event)
                                    )
                                    break
                            icloud_event.data = cal.to_ical()
                            icloud_event.save()
                            self.state['synced_events'][event_uid]['last_modified_google'] = last_modified
                            self.state['synced_events'][event_uid]['last_modified'] = last_modified
                            self._refresh_entry_times(
                                self.state['synced_events'][event_uid],
                                event['start'].get('dateTime', event['start'].get('date')),
                                _google_event_end(event),
                                event.get('summary'),
                            )
                            self.state['synced_events'][event_uid]['recurrence_sig'] = cur_rec_sig
                            # Our own write may bump iCloud's LAST-MODIFIED. Invalidate
                            # the iCloud baseline so the reverse pass re-captures the
                            # post-write timestamp instead of mistaking it for a user
                            # edit and bouncing the change back (ping-pong).
                            self.state['synced_events'][event_uid]['last_modified_icloud'] = None
                            self.state['synced_events'][event_uid].pop('icloud_lm_absent', None)
                            updated_count += 1
                            event_start = event['start'].get('dateTime', event['start'].get('date'))
                            updated_events.append({'title': event.get('summary'), 'start': event_start})
                            logger.info(f"Updated in iCloud: {event_title}")
                        except Exception as e:
                            logger.error(f"Failed to update '{event_title}' in iCloud: {e}")
                else:
                    logger.debug(f"  Skipping: Already in sync state (not modified)")
                continue

            # Check if event already exists in iCloud (by UID)
            if safe_uid in existing_icloud_events:
                # Event already exists in iCloud, just record it in state
                event_start = event['start'].get('dateTime', event['start'].get('date'))
                event_end = _google_event_end(event)
                self._record_synced_event(
                    event_uid, event.get('summary'),
                    source='google',
                    start=event_start, end=event_end,
                    icloud_uid=safe_uid,
                    last_modified_google=event.get('updated')
                )
                self.state['synced_events'][event_uid]['recurrence_sig'] = sorted(_recurrence_for(event))
                logger.debug(f"Skipped (already exists in iCloud): {event.get('summary')}")
                continue

            # Create iCloud event
            cal = Calendar()
            ical_event = Event()

            ical_event.add('summary', event.get('summary', 'No Title'))
            if event.get('description'):
                ical_event.add('description', event['description'])

            ev_start, ev_end = self._google_start_end(event)
            ical_event.add('dtstart', ev_start)
            ical_event.add('dtend', ev_end)
            ical_event.add('uid', safe_uid)

            # Carry the recurrence (RRULE/RDATE/EXDATE, plus any occurrence cancellations)
            # so a recurring series is written to iCloud as ONE recurring VEVENT, not N copies.
            self._apply_google_recurrence(ical_event, _recurrence_for(event))

            # Add 30-minute reminder
            alarm = Alarm()
            alarm.add('action', 'DISPLAY')
            alarm.add('trigger', timedelta(minutes=-30))
            alarm.add('description', 'Reminder')
            ical_event.add_component(alarm)

            cal.add_component(ical_event)

            try:
                event_start = event['start'].get('dateTime', event['start'].get('date'))
                event_end = _google_event_end(event)
                ical_data = cal.to_ical()
                retry(lambda: icloud_calendar.save_event(ical_data))
                self._record_synced_event(
                    event_uid, event.get('summary'), source='google',
                    start=event_start, end=event_end, last_modified=event.get('updated'),
                    icloud_uid=safe_uid, last_modified_google=event.get('updated')
                )
                self.state['synced_events'][event_uid]['recurrence_sig'] = sorted(_recurrence_for(event))
                synced_count += 1
                added_events.append({
                    'title': event.get('summary'),
                    'start': event_start
                })
                logger.info(f"Added to iCloud: {event.get('summary')}")
            except Exception as e:
                logger.error(f"Failed to sync '{event.get('summary')}': {e}")
                logger.debug(f"iCal data sent:\n{ical_data.decode('utf-8', errors='replace')}")
                self._record_synced_event(
                    event_uid, event.get('summary'), source='google',
                    start=event_start, end=event_end, last_modified=event.get('updated'),
                    failed=True, error=str(e)
                )
                error_count += 1

        # Detect deletions: events that were synced from Google but no longer exist
        # Only check events that fall within the current time window
        events_to_delete = []
        logger.debug(f"Checking for deleted Google events. Synced events count: {len(self.state['synced_events'])}")
        for event_id, event_info in self.state['synced_events'].items():
            if event_info.get('legacy_fanout'):
                continue  # legacy fan-out duplicate: never auto-delete (manual cleanup)
            if event_info.get('source') == 'google':
                # Check if event is within the time window
                event_start = event_info.get('start')
                logger.debug(f"Checking synced event: {event_info.get('title')} (ID: {event_id}, start: {event_start})")
                if event_start:
                    # Compare as instants, not as strings. The previous string
                    # comparison mixed 'Z' and '+00:00' forms, so the window test
                    # misfired at the boundaries. Use the same datetime window the
                    # Google query was built from (and mirror the iCloud side).
                    event_dt = _parse_instant(event_start)
                    if event_dt is None:
                        logger.debug(f"  → Could not parse date for {event_id}")
                    else:
                        window_start = start_dt
                        window_end = end_dt
                        if window_start <= event_dt <= window_end:
                            if event_id not in current_google_ids:
                                logger.debug(f"  → Marked for deletion: not in current Google events")
                                events_to_delete.append(event_id)
                            else:
                                logger.debug(f"  → Still exists in Google")
                        else:
                            logger.debug(f"  → Outside time window (event: {event_dt.isoformat()}, window: {window_start.isoformat()} to {window_end.isoformat()})")

        # Delete events from iCloud that were deleted from Google
        logger.debug(f"Events to delete from iCloud: {len(events_to_delete)}")
        for event_id in events_to_delete:
            event_info = self.state['synced_events'][event_id]
            # Match on the UID we actually WROTE to iCloud (stored as icloud_uid — it may
            # have been '_'-stripped or SHA-256 hashed for long ids). Reconstructing it from
            # the Google id failed for hashed UIDs, orphaning the iCloud event.
            target_uid = event_info.get('icloud_uid') or event_id.lstrip('_')
            logger.debug(f"Attempting to delete event from iCloud: {event_id} (uid={target_uid})")
            try:
                # Find and delete the event in iCloud by UID
                with SuppressCaldavOutput():
                    icloud_events = list(icloud_calendar.date_search(
                        start=start_dt,
                        end=end_dt,
                        expand=False
                    ))
                logger.debug(f"  Searching through {len(icloud_events)} iCloud events")
                found = False
                for icloud_event in icloud_events:
                    try:
                        ical = Calendar.from_ical(icloud_event.data)
                        for component in ical.walk():
                            if component.name == "VEVENT":
                                icloud_uid = str(component.get('uid'))
                                recurrence_id = component.get('recurrence-id')
                                # Build the same ID format we use for tracking
                                if recurrence_id:
                                    recurrence_str = recurrence_id.dt.isoformat() if hasattr(recurrence_id.dt, 'isoformat') else str(recurrence_id.dt)
                                    full_uid = f"{icloud_uid}_{recurrence_str}"
                                else:
                                    full_uid = icloud_uid
                                logger.debug(f"    Comparing: iCloud UID={full_uid} vs target={target_uid}")
                                if full_uid == target_uid or icloud_uid == target_uid:
                                    logger.debug(f"    → Match found! Deleting...")
                                    icloud_event.delete()
                                    deleted_count += 1
                                    deleted_events.append({
                                        'title': event_info['title'],
                                        'start': event_info.get('start', 'unknown')
                                    })
                                    logger.info(f"Deleted from iCloud: {event_info['title']}")
                                    del self.state['synced_events'][event_id]
                                    found = True
                                    break
                    except Exception as e:
                        logger.debug(f"    Error parsing iCloud event: {e}")
                    if found:
                        break
                if not found:
                    logger.warning(f"Could not find event {event_id} in iCloud to delete")
                    # Remove from state anyway to avoid retrying
                    if event_id in self.state['synced_events']:
                        del self.state['synced_events'][event_id]
            except Exception as e:
                logger.error(f"Error deleting event {event_id}: {e}")

        if updated_count > 0:
            logger.info(f"Updated {updated_count} event(s) in iCloud")
        if deleted_count > 0:
            logger.info(f"Deleted {deleted_count} event(s) from iCloud")

        return {
            'added': synced_count,
            'updated': updated_count,
            'deleted': deleted_count,
            'added_events': added_events,
            'updated_events': updated_events,
            'deleted_events': deleted_events,
            'writeback': 0,
            'writeback_events': [],
            'errors': error_count
        }

    def _oneway_icloud_uid(self, calendar_id, google_event_id):
        """Build a collision-safe iCloud UID for a one-way mirrored Google event.

        The mirror shares the primary iCloud calendar, so we namespace the UID with
        a short hash of the source calendar id to guarantee it can never collide with
        a primary Google event's UID (or another mirrored calendar's). Deterministic:
        the same Google event always maps to the same iCloud UID.
        """
        cal_hash = hashlib.sha256(str(calendar_id).encode()).hexdigest()[:12]
        # Strip Google-internal leading underscores that iCloud rejects.
        base = str(google_event_id).lstrip('_')
        uid = f"ow-{cal_hash}-{base}"
        # iCloud rejects UIDs longer than ~255 chars; hash overly long ones (keep prefix).
        if len(uid) > 200:
            uid = f"ow-{cal_hash}-" + hashlib.sha256(base.encode()).hexdigest()
        return uid

    def sync_google_oneway_to_icloud(self, google_service, icloud_calendar):
        """One-way mirror additional Google calendars into the (shared) iCloud calendar.

        Events are copied Google -> iCloud only, with a configurable title prefix per
        calendar. They are recorded with source='google_oneway' so they are NEVER pushed
        back to Google (see google_origin_icloud_uids in sync_icloud_to_google) and are
        untouched by the primary direction's deletion sweep (which only handles
        source=='google'). Deletions in the source Google calendar are propagated to iCloud.
        """
        oneway_calendars = self.config.get('oneway_google_calendars', []) or []
        synced_count = 0
        updated_count = 0
        deleted_count = 0
        error_count = 0
        added_events = []
        updated_events = []
        deleted_events = []

        if not oneway_calendars:
            return {
                'added': synced_count, 'updated': updated_count, 'deleted': deleted_count,
                'added_events': added_events, 'updated_events': updated_events,
                'deleted_events': deleted_events, 'writeback': 0,
                'writeback_events': [], 'errors': error_count,
            }

        logger.info(f"→ One-way mirroring {len(oneway_calendars)} additional Google calendar(s) → iCloud...")

        now = datetime.now(timezone.utc)
        time_min = (now - timedelta(days=1)).isoformat().replace('+00:00', 'Z')
        time_max = (now + timedelta(days=360)).isoformat().replace('+00:00', 'Z')

        # Fetch existing iCloud events once, keyed by UID, to detect creates vs updates
        # and to locate events for deletion. Mirror the indexing used in sync_google_to_icloud.
        existing_icloud_events = {}
        try:
            with SuppressCaldavOutput():
                icloud_events = icloud_calendar.date_search(
                    start=now - timedelta(days=1),
                    end=now + timedelta(days=360),
                    expand=True
                )
            for icloud_event in icloud_events:
                try:
                    ical = Calendar.from_ical(icloud_event.data)
                    for component in ical.walk():
                        if component.name == "VEVENT":
                            existing_icloud_events[str(component.get('uid'))] = icloud_event
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Could not fetch existing iCloud events for one-way mirror: {e}")

        for entry in oneway_calendars:
            calendar_id = entry.get('calendar_id')
            prefix = entry.get('prefix', '') or ''
            if not calendar_id:
                logger.warning("Skipping one-way calendar entry without 'calendar_id'")
                continue

            logger.info(f"  Mirroring calendar '{calendar_id}' (prefix: '{prefix}')")

            # Page all events from this source calendar
            events = []
            page_token = None
            try:
                while True:
                    events_result = google_service.events().list(
                        calendarId=calendar_id,
                        timeMin=time_min,
                        timeMax=time_max,
                        singleEvents=True,
                        orderBy='startTime',
                        pageToken=page_token
                    ).execute()
                    events.extend(events_result.get('items', []))
                    page_token = events_result.get('nextPageToken')
                    if not page_token:
                        break
            except Exception as e:
                logger.error(f"  Failed to fetch events from '{calendar_id}': {e}")
                error_count += 1
                continue

            # Deduplicate expanded recurring instances by iCalUID (one representative per series)
            seen_ical_uids = set()
            deduped_events = []
            for event in events:
                ical_uid = event.get('iCalUID', event['id'])
                if ical_uid in seen_ical_uids:
                    continue
                seen_ical_uids.add(ical_uid)
                deduped_events.append(event)
            events = deduped_events

            current_google_ids = {event['id'] for event in events}

            for event in events:
                event_uid = event['id']
                # State is keyed per source calendar to avoid collisions across calendars.
                state_key = f"ow:{calendar_id}:{event_uid}"
                safe_uid = self._oneway_icloud_uid(calendar_id, event_uid)
                base_title = event.get('summary', 'No Title')
                prefixed_title = f"{prefix}{base_title}"
                event_start = event['start'].get('dateTime', event['start'].get('date'))
                event_end = _google_event_end(event)
                # This pass expands series (singleEvents=True) and mirrors ONE
                # representative per series, so the id recorded here is an occurrence
                # id. Flag it: write-back must refuse those rather than silently edit a
                # single occurrence of the source series. Refreshed every pass so
                # entries written before this existed heal themselves.
                is_recurring = bool(event.get('recurringEventId'))

                # Already mirrored: update only if Google's timestamp changed
                if state_key in self.state['synced_events'] and not self.state['synced_events'][state_key].get('sync_failed'):
                    last_modified = event.get('updated')
                    stored_state = self.state['synced_events'][state_key]
                    stored_state['oneway_recurring'] = is_recurring
                    stored_modified = stored_state.get('last_modified_google') or stored_state.get('last_modified') or stored_state.get('synced_at')
                    if last_modified and stored_modified and last_modified != stored_modified and safe_uid in existing_icloud_events:
                        try:
                            icloud_event = existing_icloud_events[safe_uid]
                            cal = Calendar.from_ical(icloud_event.data)
                            for component in cal.walk():
                                if component.name == "VEVENT":
                                    # No recurrence: the mirror writes expanded single events.
                                    self._apply_google_fields_to_vevent(
                                        component, event, summary=prefixed_title
                                    )
                                    break
                            icloud_event.data = cal.to_ical()
                            icloud_event.save()
                            self.state['synced_events'][state_key]['last_modified_google'] = last_modified
                            self.state['synced_events'][state_key]['last_modified'] = last_modified
                            # Our own write may bump iCloud's LAST-MODIFIED. Invalidate the
                            # iCloud baseline so the reverse pass re-captures it instead of
                            # mistaking it for a user edit and writing it straight back —
                            # which, with mirror write-back enabled, loops forever.
                            self.state['synced_events'][state_key]['last_modified_icloud'] = None
                            self.state['synced_events'][state_key].pop('icloud_lm_absent', None)
                            self._refresh_entry_times(
                                self.state['synced_events'][state_key],
                                event_start, event_end, prefixed_title,
                            )
                            updated_count += 1
                            updated_events.append({'title': prefixed_title, 'start': event_start})
                            logger.info(f"  Updated in iCloud (mirror): {prefixed_title}")
                        except Exception as e:
                            logger.error(f"  Failed to update mirror '{prefixed_title}' in iCloud: {e}")
                    continue

                # Event already present in iCloud (e.g. state lost) — just record it
                if safe_uid in existing_icloud_events:
                    self._record_synced_event(
                        state_key, prefixed_title, source='google_oneway',
                        start=event_start, end=event_end, icloud_uid=safe_uid,
                        last_modified_google=event.get('updated'),
                        source_calendar_id=calendar_id
                    )
                    self.state['synced_events'][state_key]['oneway_recurring'] = is_recurring
                    continue

                # Create the mirrored iCloud event (prefixed title is the only difference)
                cal = Calendar()
                ical_event = Event()
                ical_event.add('summary', prefixed_title)
                if event.get('description'):
                    ical_event.add('description', event['description'])

                start = event['start'].get('dateTime', event['start'].get('date'))
                end = event['end'].get('dateTime', event['end'].get('date'))
                start_dt = datetime.fromisoformat(start.replace('Z', '+00:00'))
                end_dt = datetime.fromisoformat(end.replace('Z', '+00:00'))
                if hasattr(start_dt, 'tzinfo') and start_dt.tzinfo is not None:
                    start_dt = start_dt.astimezone(datetime.now().astimezone().tzinfo.utc)
                    end_dt = end_dt.astimezone(datetime.now().astimezone().tzinfo.utc)

                ical_event.add('dtstart', start_dt)
                ical_event.add('dtend', end_dt)
                ical_event.add('uid', safe_uid)

                alarm = Alarm()
                alarm.add('action', 'DISPLAY')
                alarm.add('trigger', timedelta(minutes=-30))
                alarm.add('description', 'Reminder')
                ical_event.add_component(alarm)
                cal.add_component(ical_event)

                try:
                    ical_data = cal.to_ical()
                    retry(lambda: icloud_calendar.save_event(ical_data))
                    self._record_synced_event(
                        state_key, prefixed_title, source='google_oneway',
                        start=event_start, end=event_end, last_modified=event.get('updated'),
                        icloud_uid=safe_uid, last_modified_google=event.get('updated'),
                        source_calendar_id=calendar_id
                    )
                    self.state['synced_events'][state_key]['oneway_recurring'] = is_recurring
                    synced_count += 1
                    added_events.append({'title': prefixed_title, 'start': event_start})
                    logger.info(f"  Added to iCloud (mirror): {prefixed_title}")
                except Exception as e:
                    logger.error(f"  Failed to mirror '{prefixed_title}': {e}")
                    self._record_synced_event(
                        state_key, prefixed_title, source='google_oneway',
                        start=event_start, end=event_end, last_modified=event.get('updated'),
                        failed=True, error=str(e), source_calendar_id=calendar_id
                    )
                    error_count += 1

            # Deletion propagation: mirrored events for THIS calendar no longer in Google
            events_to_delete = []
            for state_key, event_info in list(self.state['synced_events'].items()):
                if event_info.get('source') != 'google_oneway':
                    continue
                if event_info.get('source_calendar_id') != calendar_id:
                    continue
                ev_start = event_info.get('start')
                if not ev_start:
                    continue
                try:
                    event_dt = datetime.fromisoformat(ev_start.replace('Z', '+00:00'))
                except Exception:
                    continue
                if not (time_min <= event_dt.isoformat() <= time_max):
                    continue
                # state_key format is "ow:{calendar_id}:{google_event_id}". Strip the known
                # prefix (calendar_id may itself contain ':') to recover the Google event id.
                key_prefix = f"ow:{calendar_id}:"
                if not state_key.startswith(key_prefix):
                    continue
                google_event_id = state_key[len(key_prefix):]
                if google_event_id and google_event_id not in current_google_ids:
                    events_to_delete.append((state_key, event_info))

            for state_key, event_info in events_to_delete:
                target_uid = event_info.get('icloud_uid')
                try:
                    icloud_event = existing_icloud_events.get(target_uid)
                    if icloud_event is not None:
                        icloud_event.delete()
                        deleted_count += 1
                        deleted_events.append({
                            'title': event_info.get('title'),
                            'start': event_info.get('start', 'unknown')
                        })
                        logger.info(f"  Deleted from iCloud (mirror): {event_info.get('title')}")
                    else:
                        logger.warning(f"  Could not find mirrored event {target_uid} in iCloud to delete")
                    # Drop from state either way to avoid retrying forever
                    del self.state['synced_events'][state_key]
                except Exception as e:
                    logger.error(f"  Error deleting mirrored event {target_uid}: {e}")

        if synced_count or updated_count or deleted_count:
            logger.info(f"One-way mirror: {synced_count} added, {updated_count} updated, {deleted_count} deleted in iCloud")

        return {
            'added': synced_count,
            'updated': updated_count,
            'deleted': deleted_count,
            'added_events': added_events,
            'updated_events': updated_events,
            'deleted_events': deleted_events,
            'writeback': 0,
            'writeback_events': [],
            'errors': error_count
        }

    def _writeback_to_google(self, google_service, component, icloud_uid, state_key, entry,
                             last_modified, recurrence_lines, oneway_writeback, counters):
        """Push an edit made on the iCloud COPY of a Google-origin event back to Google.

        Patches the event in the calendar that actually owns it: the primary calendar for
        source='google', and its own source calendar for a one-way mirrored event — never
        the primary, and never as a new event. Follows the same baseline contract as every
        other propagation path: compare iCloud's timestamp only against the stored iCloud
        baseline, then invalidate the Google baseline so the forward pass re-captures the
        post-write timestamp instead of bouncing the change back."""
        title = str(component.get('summary', 'No Title'))
        source = entry.get('source')

        if entry.get('sync_failed') or entry.get('legacy_fanout'):
            return
        if not self._target_updates_enabled('to_google'):
            logger.debug(f"  Skipping write-back (disabled): {title}")
            return

        if source == 'google_oneway':
            calendar_id = entry.get('source_calendar_id')
            if not calendar_id:
                return
            if not oneway_writeback.get(calendar_id):
                logger.debug(f"  Skipping mirror write-back (not enabled for {calendar_id}): {title}")
                return
            if entry.get('oneway_recurring'):
                # The mirror stores an EXPANDED instance id, so a patch would silently
                # edit one occurrence of the source series. Refuse rather than surprise.
                logger.debug(f"  Skipping mirror write-back of a recurring event: {title}")
                return
            key_prefix = f"ow:{calendar_id}:"
            if not state_key.startswith(key_prefix):
                return
            google_event_id = state_key[len(key_prefix):]
            target_label = calendar_id
        else:
            calendar_id = self.config['google_calendar_id']
            # For a primary Google-origin event the state key IS the Google event id.
            google_event_id = state_key
            target_label = 'Google'
        if not google_event_id:
            return

        baseline = entry.get('last_modified_icloud')
        # iCloud does not stamp LAST-MODIFIED on the events we write, so "no baseline"
        # and "baseline is legitimately absent" both look like None. Record which one it
        # is: a timestamp APPEARING where we confirmed there was none is a user edit,
        # not our own write, and swallowing it would eat the first edit to most events.
        captured = baseline is not None or entry.get('icloud_lm_absent') is not None
        if not captured:
            # First iCloud-side baseline for this event (just mirrored, or a legacy
            # entry). Capture it and do NOT propagate — that is the ping-pong guard.
            entry['last_modified_icloud'] = last_modified
            entry['icloud_lm_absent'] = last_modified is None
            logger.debug(f"  Captured iCloud baseline for Google-origin '{title}' (no propagation)")
            return
        if entry.get('writeback_denied'):
            # Read-only calendar: keep the baseline moving so we do not re-report it
            # every cycle, but do not keep hammering a patch that cannot succeed.
            entry['last_modified_icloud'] = last_modified
            entry['icloud_lm_absent'] = last_modified is None
            return
        if not last_modified:
            return
        if baseline is not None and _same_instant(last_modified, baseline):
            return

        # The mirror writes prefixed titles into iCloud; strip the prefix before
        # patching or it compounds on every edit.
        summary = title
        if source == 'google_oneway':
            for cfg_entry in (self.config.get('oneway_google_calendars', []) or []):
                prefix = cfg_entry.get('prefix') or ''
                if cfg_entry.get('calendar_id') == calendar_id and prefix and summary.startswith(prefix):
                    summary = summary[len(prefix):]
                    break

        dtstart = component.get('dtstart').dt
        dtend_prop = component.get('dtend')
        if dtend_prop is not None:
            dtend = dtend_prop.dt
        else:
            # DTEND is optional; a VEVENT may carry DURATION instead (same fallback
            # order as _icloud_component_end).
            duration = component.get('duration')
            dtend = dtstart + duration.dt if duration is not None else dtstart
        if isinstance(dtstart, datetime):
            start_dict = {'dateTime': dtstart.isoformat(), 'timeZone': 'UTC'}
            end_dict = {'dateTime': dtend.isoformat(), 'timeZone': 'UTC'}
        else:
            start_dict = {'date': dtstart.isoformat()}
            end_dict = {'date': dtend.isoformat()}
        patch_body = {'summary': summary, 'start': start_dict, 'end': end_dict}
        if component.get('description'):
            patch_body['description'] = str(component.get('description'))
        if recurrence_lines:
            patch_body['recurrence'] = recurrence_lines

        event_start = dtstart.isoformat() if isinstance(dtstart, datetime) else str(dtstart)
        try:
            patched = retry(lambda: google_service.events().patch(
                calendarId=calendar_id,
                eventId=google_event_id,
                body=patch_body
            ).execute(), give_up=_is_permanent_http_error)
        except Exception as e:
            status = getattr(getattr(e, 'resp', None), 'status', None)
            if status in (403, 401):
                # Read-only shared calendar. Report once, then stay quiet.
                entry['writeback_denied'] = True
                entry['last_modified_icloud'] = last_modified
                logger.error(f"  Write-back to '{target_label}' denied for '{summary}' (calendar is read-only)")
                counters['errors'] += 1
            elif status in (404, 410):
                # Source event is gone; the owning direction's deletion sweep cleans up.
                logger.warning(f"  Write-back target no longer exists in '{target_label}': {summary}")
            else:
                logger.error(f"  Failed to write '{summary}' back to '{target_label}': {e}")
                counters['errors'] += 1
            return

        entry['last_modified_icloud'] = last_modified
        entry['icloud_lm_absent'] = last_modified is None
        entry['last_modified'] = last_modified
        # Our patch bumps Google's `updated`. Adopt the post-write timestamp straight
        # from the response as the new Google baseline, so neither the forward pass nor
        # the mirror reads our own write as a Google-side edit. Falling back to None
        # makes the next pass re-capture it instead — a cycle later, same outcome.
        entry['last_modified_google'] = (patched or {}).get('updated')
        if recurrence_lines:
            # Keep the recurrence signature in step, or the forward pass sees a phantom
            # recurrence change and emits a spurious "updated in iCloud".
            entry['recurrence_sig'] = sorted(recurrence_lines)
        self._refresh_entry_times(
            entry, event_start, _icloud_component_end(component, event_start), title
        )
        counters['writeback'] += 1
        counters['writeback_events'].append(
            {'title': summary, 'start': event_start, 'target': target_label}
        )
        logger.info(f"Written back to {target_label}: {summary}")

    def sync_icloud_to_google(self, google_service, icloud_calendar):
        """Sync events from iCloud to Google Calendar"""
        logger.info("← Syncing iCloud → Google...")

        now, start, end, time_min, time_max = self._sync_window()

        with SuppressCaldavOutput():
            events = icloud_calendar.date_search(start=start, end=end, expand=False)
        synced_count = 0
        updated_count = 0
        deleted_count = 0
        error_count = 0
        added_events = []
        updated_events = []
        deleted_events = []
        # Mutable tally shared with _writeback_to_google (edits made on the iCloud copy
        # of a Google-origin event, pushed back to whichever Google calendar owns it).
        counters = {'writeback': 0, 'writeback_events': [], 'errors': 0}

        # Track current iCloud event UIDs
        current_icloud_ids = set()

        # Build an index of iCloud UIDs that were written by us from Google-origin events
        # (including hashed UIDs), mapped back to their state key. Used to prevent
        # iCloud→Google ping-pong, and to locate the Google event to patch when an edit
        # made on the iCloud copy has to be written back. The state key is NOT always the
        # iCloud UID (Google ids starting with '_' are stripped, long ones hashed, and a
        # foreign iCalUID may have been adopted), which is why this reverse map exists.
        google_origin_by_icloud_uid = {
            entry['icloud_uid']: (state_key, entry)
            for state_key, entry in self.state['synced_events'].items()
            if entry.get('source') in ('google', 'google_oneway') and entry.get('icloud_uid')
        }
        google_origin_icloud_uids = set(google_origin_by_icloud_uid)
        # Per-calendar write-back opt-in for one-way mirrored calendars.
        oneway_writeback = {
            entry.get('calendar_id'): bool(entry.get('writeback'))
            for entry in (self.config.get('oneway_google_calendars', []) or [])
        }

        # Get existing Google events to check for duplicates
        existing_google_events = {}
        try:
            logger.debug(f"Querying Google Calendar: {self.config['google_calendar_id']}")
            logger.debug(f"Time range: {time_min} to {time_max}")

            page_token = None
            all_google_events = []
            while True:
                google_events_result = google_service.events().list(
                    calendarId=self.config['google_calendar_id'],
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=False,
                    pageToken=page_token
                ).execute()
                all_google_events.extend(google_events_result.get('items', []))
                page_token = google_events_result.get('nextPageToken')
                if not page_token:
                    break
            for g_event in all_google_events:
                # Use iCalUID if available (for events synced from iCloud), otherwise use event ID
                # iCalUID will be in format: "uid" or "uid_recurrence-datetime" for recurring instances
                event_uid = g_event.get('iCalUID', g_event['id'])
                existing_google_events[event_uid] = g_event
                # Also index by stripped UID: Google events synced from iCloud via this app
                # have their iCloud UID set as iCalUID (with leading '_' stripped). When iCloud
                # returns the event, its UID is the stripped form — so we need both keys to
                # avoid re-syncing the same event back to Google.
                stripped_uid = event_uid.lstrip('_')
                if stripped_uid != event_uid:
                    existing_google_events[stripped_uid] = g_event
                # Native Google events have iCalUID like "abc123@google.com" but iCloud stores
                # them with the bare UID "abc123" — index by both to avoid re-syncing.
                if '@google.com' in event_uid:
                    bare_uid = event_uid.split('@google.com')[0]
                    existing_google_events[bare_uid] = g_event
        except Exception as e:
            logger.warning(f"Could not fetch existing Google events: {e}")

        # Add new events
        logger.debug(f"Processing {len(events)} iCloud events...")
        for event in events:
            try:
                ical = Calendar.from_ical(event.data)
            except Exception as e:
                logger.error(f"Error parsing event: {e}")
                continue

            for component in ical.walk():
                if component.name == "VEVENT":
                    uid = str(component.get('uid'))

                    # expand=False → masters carry RRULE, single events stand alone, and a
                    # modified occurrence appears as a RECURRENCE-ID override VEVENT. Per the
                    # chosen recurring depth, per-instance overrides are not synced as separate
                    # Google events (occurrence cancellations ride on the master's EXDATE).
                    if component.get('recurrence-id'):
                        logger.debug(f"  Skipping iCloud recurrence override (not synced separately): {uid}")
                        continue
                    event_id = uid

                    current_icloud_ids.add(event_id)

                    # Google recurrence[] strings for this event (empty for non-recurring).
                    recurrence_lines = self._extract_ical_recurrence(component)

                    # Get iCloud last-modified timestamp
                    last_modified_prop = component.get('last-modified')
                    last_modified = last_modified_prop.dt.isoformat() if last_modified_prop and hasattr(last_modified_prop.dt, 'isoformat') else None

                    # Resolve which side this event originated on BEFORE the generic
                    # update branch. A Google-origin event whose iCloud UID happens to
                    # equal its state key used to fall into that branch by accident and
                    # be written back unconditionally, while the same edit on an event
                    # with a rewritten UID was silently dropped. Both now take the
                    # explicit, flag-gated write-back path below.
                    wb_key, wb_entry = None, None
                    if event_id in self.state['synced_events']:
                        entry = self.state['synced_events'][event_id]
                        if entry.get('source') in ('google', 'google_oneway'):
                            wb_key, wb_entry = event_id, entry
                    elif event_id in google_origin_by_icloud_uid:
                        wb_key, wb_entry = google_origin_by_icloud_uid[event_id]

                    if wb_entry is not None:
                        self._writeback_to_google(
                            google_service, component, event_id, wb_key, wb_entry,
                            last_modified, recurrence_lines, oneway_writeback, counters,
                        )
                        continue

                    if event_id in self.state['synced_events']:
                        # Propagate only if iCloud's OWN timestamp advanced past the
                        # stored iCloud baseline. Compare against the iCloud baseline
                        # only — never fall back to the Google timestamp.
                        stored_state = self.state['synced_events'][event_id]
                        baseline = stored_state.get('last_modified_icloud')
                        if baseline is None:
                            # First time we have an iCloud-side baseline for this
                            # event (typically a Google-origin event we just
                            # mirrored, or a legacy entry). Capture it and do NOT
                            # propagate — this is what caused the update ping-pong.
                            stored_state['last_modified_icloud'] = last_modified
                            logger.debug(f"  Captured iCloud baseline for '{component.get('summary')}' (no propagation)")
                            continue
                        if last_modified and not _same_instant(last_modified, baseline):
                            event_title = str(component.get('summary', 'No Title'))
                            logger.debug(f"  Event modified: {event_title} (was: {baseline}, now: {last_modified})")
                            # Find the Google event and update it
                            g_event = existing_google_events.get(event_id)
                            if g_event:
                                try:
                                    dtstart = component.get('dtstart').dt
                                    dtend = component.get('dtend').dt
                                    if isinstance(dtstart, datetime):
                                        start_dict = {'dateTime': dtstart.isoformat(), 'timeZone': 'UTC'}
                                        end_dict = {'dateTime': dtend.isoformat(), 'timeZone': 'UTC'}
                                    else:
                                        start_dict = {'date': dtstart.isoformat()}
                                        end_dict = {'date': dtend.isoformat()}
                                    patch_body = {
                                        'summary': str(component.get('summary', 'No Title')),
                                        'start': start_dict,
                                        'end': end_dict,
                                    }
                                    if component.get('description'):
                                        patch_body['description'] = str(component.get('description'))
                                    # Keep the series' recurrence in sync (RRULE/EXDATE edits).
                                    if recurrence_lines:
                                        patch_body['recurrence'] = recurrence_lines
                                    google_service.events().patch(
                                        calendarId=self.config['google_calendar_id'],
                                        eventId=g_event['id'],
                                        body=patch_body
                                    ).execute()
                                    self.state['synced_events'][event_id]['last_modified_icloud'] = last_modified
                                    self.state['synced_events'][event_id]['last_modified'] = last_modified
                                    self.state['synced_events'][event_id]['title'] = event_title
                                    # Our patch bumps Google's `updated`. Invalidate the
                                    # Google baseline so the forward pass re-captures the
                                    # post-write timestamp instead of bouncing it back.
                                    self.state['synced_events'][event_id]['last_modified_google'] = None
                                    updated_count += 1
                                    event_start = dtstart.isoformat() if isinstance(dtstart, datetime) else str(dtstart)
                                    updated_events.append({'title': event_title, 'start': event_start})
                                    logger.info(f"Updated in Google: {event_title}")
                                except Exception as e:
                                    logger.error(f"Failed to update '{event_title}' in Google: {e}")
                        else:
                            logger.debug(f"  Skipping: Already in sync state (not modified)")
                        continue

                    # Check if this iCloud UID is the hashed form of a Google-origin event.
                    # When a Google event has a very long UID, we hash it before writing to iCloud.
                    # The iCloud→Google direction sees the hashed UID and wouldn't find it in
                    # existing_google_events, causing it to add the event back to Google as new.
                    # A one-way mirrored event carries a UID we minted ourselves. Recognise
                    # it structurally rather than trusting its state entry to still be
                    # there: losing that entry is exactly what let mirrored events get
                    # pushed back into the primary Google calendar.
                    if ONEWAY_UID_RE.match(str(event_id)):
                        logger.debug(f"  Skipping: one-way mirrored event, never synced back: {component.get('summary')}")
                        continue

                    if event_id in google_origin_icloud_uids:
                        logger.debug(f"  Skipping: iCloud event with hashed UID originated from Google: {component.get('summary')}")
                        continue

                    # Check if event already exists in Google (by UID)
                    if event_id in existing_google_events:
                        # Event already exists in Google, just record it in state
                        dtstart = component.get('dtstart').dt
                        event_start = dtstart.isoformat() if isinstance(dtstart, datetime) else str(dtstart)
                        event_end = _icloud_component_end(component, event_start)
                        self._record_synced_event(
                            event_id,
                            str(component.get('summary')),
                            source='icloud', start=event_start, end=event_end, last_modified=last_modified,
                            last_modified_icloud=last_modified
                        )
                        logger.debug(f"Skipped (already exists in Google): {component.get('summary')}")
                        continue

                    dtstart = component.get('dtstart').dt
                    dtend = component.get('dtend').dt

                    # Handle all-day events
                    if isinstance(dtstart, datetime):
                        start_dict = {
                            'dateTime': dtstart.isoformat(),
                            'timeZone': 'UTC',
                        }
                        end_dict = {
                            'dateTime': dtend.isoformat(),
                            'timeZone': 'UTC',
                        }
                    else:
                        start_dict = {'date': dtstart.isoformat()}
                        end_dict = {'date': dtend.isoformat()}

                    google_event = {
                        'summary': str(component.get('summary', 'No Title')),
                        'start': start_dict,
                        'end': end_dict,
                        'iCalUID': event_id,  # Preserve the original UID
                        'reminders': {
                            'useDefault': False,
                            'overrides': [
                                {'method': 'popup', 'minutes': 30},
                            ]
                        }
                    }

                    if component.get('description'):
                        google_event['description'] = str(component.get('description'))
                    # Write the series as a real recurring Google event (one master), not
                    # N expanded copies. Empty for non-recurring events.
                    if recurrence_lines:
                        google_event['recurrence'] = recurrence_lines

                    event_start = dtstart.isoformat() if isinstance(dtstart, datetime) else str(dtstart)
                    event_end = _icloud_component_end(component, event_start)
                    event_title = str(component.get('summary'))
                    logger.debug(f"Adding event to Google: {google_event['summary']} ({start_dict})")

                    try:
                        retry(lambda: google_service.events().insert(
                            calendarId=self.config['google_calendar_id'],
                            body=google_event
                        ).execute())

                        self._record_synced_event(
                            event_id, event_title, source='icloud',
                            start=event_start, end=event_end, last_modified=last_modified,
                            last_modified_icloud=last_modified
                        )
                        synced_count += 1
                        added_events.append({
                            'title': event_title,
                            'start': event_start
                        })
                        logger.info(f"Added to Google: {event_title}")
                    except Exception as e:
                        from googleapiclient.errors import HttpError as _HttpError
                        if isinstance(e, _HttpError) and e.resp.status == 409:
                            logger.warning(f"Event already exists in Google (409), attempting update: {event_title}")
                            logger.debug(f"409 lookup: event_id={event_id!r}")
                            # Try exact UID, then stripped variants
                            g_event = (
                                existing_google_events.get(event_id)
                                or existing_google_events.get(event_id.lstrip('_'))
                                or existing_google_events.get(event_id.split('@google.com')[0])
                            )
                            if g_event is None:
                                # Last resort: fetch the event directly from Google by iCalUID
                                logger.debug(f"Not in local cache, fetching from Google by iCalUID: {event_id}")
                                try:
                                    result = google_service.events().list(
                                        calendarId=self.config['google_calendar_id'],
                                        iCalUID=event_id,
                                        singleEvents=False,
                                    ).execute()
                                    items = result.get('items', [])
                                    if items:
                                        g_event = items[0]
                                        logger.debug(f"Found via iCalUID lookup: id={g_event['id']} iCalUID={g_event.get('iCalUID')}")
                                    else:
                                        logger.debug(f"iCalUID lookup returned no results for {event_id!r}")
                                except Exception as lookup_err:
                                    logger.error(f"iCalUID lookup failed: {lookup_err}")
                            if g_event:
                                try:
                                    patch_body = {
                                        'summary': google_event['summary'],
                                        'start': start_dict,
                                        'end': end_dict,
                                    }
                                    if google_event.get('description'):
                                        patch_body['description'] = google_event['description']
                                    retry(lambda: google_service.events().patch(
                                        calendarId=self.config['google_calendar_id'],
                                        eventId=g_event['id'],
                                        body=patch_body
                                    ).execute())
                                    updated_count += 1
                                    logger.info(f"Updated existing Google event: {event_title}")
                                except Exception as patch_err:
                                    logger.error(f"Failed to update existing Google event '{event_title}': {patch_err}")
                            else:
                                logger.warning(f"409 but could not locate event in Google even by iCalUID lookup: {event_title} ({event_id})")
                            self._record_synced_event(
                                event_id, event_title, source='icloud',
                                start=event_start, end=event_end, last_modified=last_modified
                            )
                        else:
                            logger.error(f"Failed to add event to Google: {e}")
                            self._record_synced_event(
                                event_id, event_title, source='icloud',
                                start=event_start, end=event_end, last_modified=last_modified,
                                failed=True, error=str(e)
                            )
                            error_count += 1

        # Detect deletions: events that were synced from iCloud but no longer exist
        # Only check events that fall within the current time window
        events_to_delete = []
        logger.debug(f"Checking for deleted iCloud events. Synced events count: {len(self.state['synced_events'])}")
        logger.debug(f"Current iCloud IDs: {current_icloud_ids}")
        for event_id, event_info in self.state['synced_events'].items():
            if event_info.get('legacy_fanout'):
                continue  # legacy fan-out duplicate: never auto-delete (manual cleanup)
            if event_info.get('source') == 'icloud':
                # Check if event is within the time window
                event_start = event_info.get('start')
                logger.debug(f"Checking synced event: {event_info.get('title')} (ID: {event_id}, start: {event_start})")
                if event_start:
                    try:
                        # Parse the event start time
                        event_dt = datetime.fromisoformat(event_start.replace('Z', '+00:00'))
                        # Convert to UTC for comparison
                        if event_dt.tzinfo is not None:
                            event_dt = event_dt.astimezone(timezone.utc)
                        else:
                            # Assume UTC if no timezone
                            event_dt = event_dt.replace(tzinfo=timezone.utc)
                        # Only consider for deletion if within our query window
                        if start <= event_dt <= end:
                            if event_id not in current_icloud_ids:
                                logger.debug(f"  → Marked for deletion: not in current iCloud events")
                                events_to_delete.append(event_id)
                            else:
                                logger.debug(f"  → Still exists in iCloud")
                        else:
                            logger.debug(f"  → Outside time window (event: {event_dt}, window: {start} to {end})")
                    except Exception as e:
                        # If we can't parse the date, skip this event
                        logger.debug(f"  → Could not parse date: {e}")

        # Delete events from Google that were deleted from iCloud
        logger.debug(f"Events to delete from Google: {len(events_to_delete)}")
        for event_id in events_to_delete:
            logger.debug(f"Attempting to delete event from Google: {event_id}")
            try:
                # event_id is the iCalUID (iCloud UID); look up Google's own event ID
                # from the already-fetched google events map (keyed by iCalUID)
                g_event = existing_google_events.get(event_id)
                if not g_event:
                    logger.warning(f"Could not find Google event for iCalUID {event_id}, skipping delete")
                    del self.state['synced_events'][event_id]
                    continue
                google_event_id = g_event['id']
                retry(lambda: google_service.events().delete(
                    calendarId=self.config['google_calendar_id'],
                    eventId=google_event_id
                ).execute())
                deleted_count += 1
                event_info = self.state['synced_events'][event_id]
                deleted_events.append({
                    'title': event_info['title'],
                    'start': event_info.get('start', 'unknown')
                })
                logger.info(f"Deleted from Google: {event_info['title']}")
                del self.state['synced_events'][event_id]
            except Exception as e:
                # Event might already be deleted or not found - keep in state to retry later
                logger.warning(f"Could not delete event {event_id} from Google: {e}")

        if updated_count > 0:
            logger.info(f"Updated {updated_count} event(s) in Google")
        if counters['writeback'] > 0:
            logger.info(f"Wrote back {counters['writeback']} iCloud edit(s) to Google")
        if deleted_count > 0:
            logger.info(f"Deleted {deleted_count} event(s) from Google")

        return {
            'added': synced_count,
            'updated': updated_count,
            'deleted': deleted_count,
            'added_events': added_events,
            'updated_events': updated_events,
            'deleted_events': deleted_events,
            'writeback': counters['writeback'],
            'writeback_events': counters['writeback_events'],
            'errors': error_count + counters['errors']
        }

    def run_reconcile(self):
        """Run a reconciliation check and send a summary notification."""
        logger.info("Running daily reconciliation check...")
        try:
            # Import reconcile lazily to avoid circular deps and keep it optional
            import importlib.util, pathlib
            spec = importlib.util.spec_from_file_location(
                "reconcile",
                pathlib.Path(__file__).parent / "reconcile.py"
            )
            reconcile_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(reconcile_mod)

            results = reconcile_mod.reconcile()

            matching = len(results['in_both_match']) if 'in_both_match' in results else 0
            mismatched = len(results['in_both_mismatch'])
            google_only = len(results['only_in_google'])
            icloud_only = len(results['only_in_icloud'])

            lines = [f"Daily reconcile: {matching} matching, {mismatched} mismatched, {google_only} Google-only, {icloud_only} iCloud-only"]

            if mismatched:
                lines.append(f"Mismatches ({mismatched}):")
                for uid, g_ev, i_comp, diffs in results['in_both_mismatch'][:5]:
                    title = g_ev.get('summary', 'No Title')
                    lines.append(f"  ~ {title}: {'; '.join(diffs)}")
                if mismatched > 5:
                    lines.append(f"  ... and {mismatched - 5} more")

            if google_only:
                lines.append(f"Google-only ({google_only}):")
                for uid in results['only_in_google'][:5]:
                    g_ev = results['google_by_uid'][uid]
                    start = g_ev['start'].get('dateTime', g_ev['start'].get('date', ''))[:10]
                    lines.append(f"  → {g_ev.get('summary', 'No Title')} ({start})")
                if google_only > 5:
                    lines.append(f"  ... and {google_only - 5} more")

            if icloud_only:
                lines.append(f"iCloud-only ({icloud_only}):")
                for uid in results['only_in_icloud'][:5]:
                    i_comp = results['icloud_by_uid'][uid]
                    i_start, _ = reconcile_mod.icloud_event_times(i_comp)
                    start = (i_start or '')[:10]
                    lines.append(f"  ← {i_comp.get('summary', 'No Title')} ({start})")
                if icloud_only > 5:
                    lines.append(f"  ... and {icloud_only - 5} more")

            self.state['last_reconcile'] = datetime.now().isoformat()
            self.save_state()

            notification_text = "\n".join(lines)
            logger.debug("Reconcile results:\n" + notification_text)

            if mismatched or google_only or icloud_only:
                self.send_notification("Calendar Reconcile", notification_text)
            else:
                logger.info("Reconcile: all calendars in sync, no notification sent")

        except Exception as e:
            logger.error(f"Reconcile failed: {e}")
            logger.debug("Full traceback:", exc_info=True)
            self.send_notification("Calendar Reconcile Error", f"Reconcile failed: {e}")

    def run_sync(self):
        """Execute bidirectional sync"""
        start_time = time.time()
        try:
            logger.info("="*60)
            logger.info(f"Starting sync at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            logger.info("="*60)

            google_service = self.get_google_service()
            icloud_calendar = self.get_icloud_calendar()

            google_result = self.sync_google_to_icloud(google_service, icloud_calendar)
            # One-way mirror runs BEFORE the iCloud→Google direction so its state entries
            # (source='google_oneway') are already recorded when that direction builds its
            # back-sync exclusion set — guaranteeing mirrored events are never pushed to Google.
            oneway_result = self.sync_google_oneway_to_icloud(google_service, icloud_calendar)
            icloud_result = self.sync_icloud_to_google(google_service, icloud_calendar)

            self.state['last_sync'] = datetime.now().isoformat()
            # Clear last error on successful sync
            if self.state.get('last_error'):
                self.state['last_error'] = None
            self._cleanup_past_events()
            self.save_state()

            total_added = google_result['added'] + icloud_result['added'] + oneway_result['added']
            total_updated = google_result.get('updated', 0) + icloud_result.get('updated', 0) + oneway_result.get('updated', 0)
            # Edits made on the iCloud copy and pushed back to the calendar that owns them.
            total_writeback = icloud_result.get('writeback', 0)
            total_updated += total_writeback
            total_deleted = google_result['deleted'] + icloud_result['deleted'] + oneway_result['deleted']
            total_errors = google_result.get('errors', 0) + icloud_result.get('errors', 0) + oneway_result.get('errors', 0)

            # Calculate elapsed time
            elapsed = time.time() - start_time
            if elapsed < 60:
                time_str = f"{elapsed:.1f}s"
            else:
                minutes = int(elapsed // 60)
                seconds = elapsed % 60
                time_str = f"{minutes}m {seconds:.1f}s"

            message = f"Sync complete: {google_result['added']} added from Google, {icloud_result['added']} added from iCloud, {google_result.get('updated', 0)} updated in iCloud, {icloud_result.get('updated', 0)} updated in Google, {google_result['deleted']} deleted from iCloud, {icloud_result['deleted']} deleted from Google"
            if oneway_result['added'] or oneway_result.get('updated', 0) or oneway_result['deleted']:
                message += f", mirror: {oneway_result['added']} added / {oneway_result.get('updated', 0)} updated / {oneway_result['deleted']} deleted in iCloud"
            if total_writeback:
                message += f", {total_writeback} written back to Google"
            message += f", {total_errors} errors occurred in {time_str}"
            logger.info(message)

            # Send notification if any events were added, updated, or deleted
            if total_added > 0 or total_updated > 0 or total_deleted > 0 or total_errors > 0:
                notification_parts = []

                # Added events from Google
                if google_result['added'] > 0:
                    notification_parts.append(f"Added {google_result['added']} from Google:")
                    for evt in google_result['added_events'][:5]:  # Limit to 5 events
                        date_str = evt['start'][:10] if len(evt['start']) > 10 else evt['start']
                        notification_parts.append(f"  + {evt['title']} ({date_str})")
                    if google_result['added'] > 5:
                        notification_parts.append(f"  ... and {google_result['added'] - 5} more")

                # Added events from iCloud
                if icloud_result['added'] > 0:
                    notification_parts.append(f"Added {icloud_result['added']} from iCloud:")
                    for evt in icloud_result['added_events'][:5]:
                        date_str = evt['start'][:10] if len(evt['start']) > 10 else evt['start']
                        notification_parts.append(f"  + {evt['title']} ({date_str})")
                    if icloud_result['added'] > 5:
                        notification_parts.append(f"  ... and {icloud_result['added'] - 5} more")

                # Updated events in iCloud (from Google changes)
                if google_result.get('updated', 0) > 0:
                    notification_parts.append(f"Updated {google_result['updated']} in iCloud:")
                    for evt in google_result['updated_events'][:5]:
                        date_str = evt['start'][:10] if len(evt['start']) > 10 else evt['start']
                        notification_parts.append(f"  ~ {evt['title']} ({date_str})")
                    if google_result['updated'] > 5:
                        notification_parts.append(f"  ... and {google_result['updated'] - 5} more")

                # Updated events in Google (from iCloud changes)
                if icloud_result.get('updated', 0) > 0:
                    notification_parts.append(f"Updated {icloud_result['updated']} in Google:")
                    for evt in icloud_result['updated_events'][:5]:
                        date_str = evt['start'][:10] if len(evt['start']) > 10 else evt['start']
                        notification_parts.append(f"  ~ {evt['title']} ({date_str})")
                    if icloud_result['updated'] > 5:
                        notification_parts.append(f"  ... and {icloud_result['updated'] - 5} more")

                # Edits made on the iCloud copy of a Google-owned event, written back to
                # the calendar that owns it. Kept separate from "Updated in Google" above,
                # which is about iCloud-owned events, so the direction stays readable.
                if icloud_result.get('writeback', 0) > 0:
                    by_target = {}
                    for evt in icloud_result['writeback_events']:
                        by_target.setdefault(evt.get('target', 'Google'), []).append(evt)
                    for target, evts in by_target.items():
                        notification_parts.append(f"Written back {len(evts)} to {target}:")
                        for evt in evts[:5]:
                            date_str = evt['start'][:10] if len(evt['start']) > 10 else evt['start']
                            notification_parts.append(f"  \u21a9 {evt['title']} ({date_str})")
                        if len(evts) > 5:
                            notification_parts.append(f"  ... and {len(evts) - 5} more")

                # One-way mirrored events (Google → iCloud, prefixed)
                if oneway_result['added'] > 0:
                    notification_parts.append(f"Mirrored {oneway_result['added']} to iCloud:")
                    for evt in oneway_result['added_events'][:5]:
                        date_str = evt['start'][:10] if len(evt['start']) > 10 else evt['start']
                        notification_parts.append(f"  + {evt['title']} ({date_str})")
                    if oneway_result['added'] > 5:
                        notification_parts.append(f"  ... and {oneway_result['added'] - 5} more")
                if oneway_result.get('updated', 0) > 0:
                    notification_parts.append(f"Updated {oneway_result['updated']} mirrored in iCloud:")
                    for evt in oneway_result['updated_events'][:5]:
                        date_str = evt['start'][:10] if len(evt['start']) > 10 else evt['start']
                        notification_parts.append(f"  ~ {evt['title']} ({date_str})")
                    if oneway_result['updated'] > 5:
                        notification_parts.append(f"  ... and {oneway_result['updated'] - 5} more")
                if oneway_result['deleted'] > 0:
                    notification_parts.append(f"Deleted {oneway_result['deleted']} mirrored from iCloud:")
                    for evt in oneway_result['deleted_events'][:5]:
                        date_str = evt['start'][:10] if len(evt['start']) > 10 else evt['start']
                        notification_parts.append(f"  - {evt['title']} ({date_str})")
                    if oneway_result['deleted'] > 5:
                        notification_parts.append(f"  ... and {oneway_result['deleted'] - 5} more")

                # Deleted events from Google
                if google_result['deleted'] > 0:
                    notification_parts.append(f"Deleted {google_result['deleted']} from iCloud:")
                    for evt in google_result['deleted_events'][:5]:
                        date_str = evt['start'][:10] if len(evt['start']) > 10 else evt['start']
                        notification_parts.append(f"  - {evt['title']} ({date_str})")
                    if google_result['deleted'] > 5:
                        notification_parts.append(f"  ... and {google_result['deleted'] - 5} more")

                # Deleted events from iCloud
                if icloud_result['deleted'] > 0:
                    notification_parts.append(f"Deleted {icloud_result['deleted']} from Google:")
                    for evt in icloud_result['deleted_events'][:5]:
                        date_str = evt['start'][:10] if len(evt['start']) > 10 else evt['start']
                        notification_parts.append(f"  - {evt['title']} ({date_str})")
                    if icloud_result['deleted'] > 5:
                        notification_parts.append(f"  ... and {icloud_result['deleted'] - 5} more")

                # Include error count if any errors occurred
                if google_result['errors'] > 0:
                    notification_parts.append(f"  ✗ {google_result['errors']} errors occurred from Google")
                if icloud_result['errors'] > 0:
                    notification_parts.append(f"  ✗ {icloud_result['errors']} errors occurred from iCloud")
                if oneway_result['errors'] > 0:
                    notification_parts.append(f"  ✗ {oneway_result['errors']} errors occurred in one-way mirror")

                notification_message = "\n".join(notification_parts)
                self.send_notification("Calendar Sync", notification_message)
            else:
                # No changes — check if heartbeat notification is due
                last_notified = self.state.get('last_notification_sent')
                heartbeat_due = True
                if last_notified:
                    try:
                        last_dt = datetime.fromisoformat(last_notified)
                        if last_dt.tzinfo is None:
                            last_dt = last_dt.replace(tzinfo=timezone.utc)
                        seconds_since = (datetime.now(timezone.utc) - last_dt).total_seconds()
                        heartbeat_due = seconds_since >= HEARTBEAT_INTERVAL
                    except Exception:
                        heartbeat_due = True
                if heartbeat_due:
                    current_hour = datetime.now().hour
                    if HEARTBEAT_DAY_START <= current_hour < HEARTBEAT_DAY_END:
                        logger.info("Sending heartbeat notification (no changes, interval elapsed)")
                        self.send_notification("Calendar Sync", f"Heartbeat: sync is running normally, no changes in the last {HEARTBEAT_INTERVAL // 3600}h")
                        self.save_state()
                    else:
                        logger.debug(f"Heartbeat due but outside day hours ({HEARTBEAT_DAY_START}:00-{HEARTBEAT_DAY_END}:00), skipping")

            return True

        except Exception as e:
            # Calculate elapsed time for error case
            elapsed = time.time() - start_time
            if elapsed < 60:
                time_str = f"{elapsed:.1f}s"
            else:
                minutes = int(elapsed // 60)
                seconds = elapsed % 60
                time_str = f"{minutes}m {seconds:.1f}s"

            error_msg = f"Sync failed after {time_str}: {str(e)}"
            logger.error(error_msg)
            logger.debug("Full traceback:", exc_info=True)

            # Only send notification if this is a new/different error
            last_error = self.state.get('last_error')
            if last_error != error_msg:
                self.send_notification("Calendar Sync Error", error_msg)
                self.state['last_error'] = error_msg
                self.save_state()

            return False

def main():
    """Main loop with scheduled syncing"""
    logger.info("Calendar Sync Service Starting...")
    logger.info(f"Sync interval: {SYNC_INTERVAL} seconds (log level: {LOG_LEVEL})")
    logger.info(f"Daily reconcile scheduled at {RECONCILE_HOUR:02d}:00")

    sync = CalendarSync()

    while True:
        sync.run_sync()

        # Run daily reconcile once per day at RECONCILE_HOUR
        now = datetime.now()
        last_reconcile_str = sync.state.get('last_reconcile')
        run_reconcile = False
        if now.hour == RECONCILE_HOUR:
            if last_reconcile_str:
                try:
                    last_reconcile = datetime.fromisoformat(last_reconcile_str)
                    if (now - last_reconcile).total_seconds() > 3600:
                        run_reconcile = True
                except Exception:
                    run_reconcile = True
            else:
                run_reconcile = True
        if run_reconcile:
            sync.run_reconcile()

        logger.info(f"Next sync in {SYNC_INTERVAL} seconds...")
        time.sleep(SYNC_INTERVAL)

if __name__ == "__main__":
    main()
