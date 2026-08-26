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
from io import StringIO
from datetime import datetime, timedelta, timezone
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
CONFIG_FILE = '/app/data/config.json'
TOKEN_FILE = '/app/data/token.pickle'
STATE_FILE = '/app/data/sync_state.json'
CREDENTIALS_FILE = '/app/data/credentials.json'

SCOPES = ['https://www.googleapis.com/auth/calendar']
SYNC_INTERVAL = int(os.getenv('SYNC_INTERVAL', '900'))  # 15 minutes default
HEARTBEAT_INTERVAL = int(os.getenv('HEARTBEAT_INTERVAL', '172800'))  # 48 hours default
HEARTBEAT_DAY_START = int(os.getenv('HEARTBEAT_DAY_START', '8'))   # hour (0-23), inclusive
HEARTBEAT_DAY_END = int(os.getenv('HEARTBEAT_DAY_END', '22'))       # hour (0-23), exclusive
RECONCILE_HOUR = int(os.getenv('RECONCILE_HOUR', '14'))  # hour of day to run daily reconcile
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()  # DEBUG, INFO, WARNING, ERROR

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

def retry(fn, retries=3, delay=5, backoff=2):
    """Call fn(), retrying on exception with exponential backoff."""
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            if attempt == retries - 1:
                raise
            wait = delay * (backoff ** attempt)
            logger.warning(f"Request failed ({e}), retrying in {wait}s (attempt {attempt + 1}/{retries})...")
            time.sleep(wait)

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

    def load_state(self):
        """Load sync state"""
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        return {'last_sync': None, 'synced_events': {}, 'last_error': None, 'last_notification_sent': None}

    def save_state(self):
        """Save sync state"""
        with open(STATE_FILE, 'w') as f:
            json.dump(self.state, f, indent=2)

    def _cleanup_past_events(self):
        """Remove past events from sync state to keep it lean"""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).date()
        to_remove = []
        for event_id, event_info in self.state['synced_events'].items():
            event_start = event_info.get('start')
            if not event_start:
                continue
            try:
                dt = datetime.fromisoformat(event_start.replace('Z', '+00:00'))
                event_date = dt.date() if isinstance(dt, datetime) else dt
                if event_date < cutoff:
                    to_remove.append(event_id)
            except Exception:
                pass
        for event_id in to_remove:
            del self.state['synced_events'][event_id]
        if to_remove:
            logger.debug(f"Removed {len(to_remove)} past event(s) from sync state")

    def _record_synced_event(self, event_id, title, source, start, last_modified=None, failed=False, error=None, icloud_uid=None, last_modified_google=None, last_modified_icloud=None, source_calendar_id=None):
        """Record a synced event in the state"""
        existing = self.state['synced_events'].get(event_id, {})
        entry = {
            'title': title,
            'synced_at': datetime.now().isoformat(),
            'source': source,
            'start': start,
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

        now = datetime.now(timezone.utc)
        time_min = (now - timedelta(days=1)).isoformat().replace('+00:00', 'Z')
        time_max = (now + timedelta(days=360)).isoformat().replace('+00:00', 'Z')

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

    def sync_google_to_icloud(self, google_service, icloud_calendar):

        """Sync events from Google Calendar to iCloud"""
        logger.info("→ Syncing Google → iCloud...")

        now = datetime.now(timezone.utc)
        time_min = (now - timedelta(days=1)).isoformat().replace('+00:00', 'Z')
        time_max = (now + timedelta(days=360)).isoformat().replace('+00:00', 'Z')

        logger.debug(f"Querying Google Calendar: {self.config['google_calendar_id']}")
        logger.debug(f"Time range: {time_min} to {time_max}")

        events = []
        page_token = None
        while True:
            events_result = google_service.events().list(
                calendarId=self.config['google_calendar_id'],
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

        logger.info(f"Fetched {len(events)} events from Google Calendar")
        logger.debug(f"Number of events in 'items': {len(events)}")
        synced_count = 0
        updated_count = 0
        deleted_count = 0
        error_count = 0
        added_events = []
        updated_events = []
        deleted_events = []

        # Deduplicate recurring event instances by iCalUID — singleEvents=True expands
        # each occurrence of a recurring series into its own event with a unique composite ID.
        # We only want to sync one representative instance per series (the first one returned,
        # which is the earliest upcoming occurrence) to avoid flooding iCloud with duplicates.
        seen_ical_uids = set()
        deduped_events = []
        for event in events:
            ical_uid = event.get('iCalUID', event['id'])
            if ical_uid in seen_ical_uids:
                logger.debug(f"  Skipping duplicate recurring instance: {event.get('summary')} (iCalUID: {ical_uid})")
                continue
            seen_ical_uids.add(ical_uid)
            deduped_events.append(event)
        if len(deduped_events) < len(events):
            logger.debug(f"Deduplicated {len(events)} events to {len(deduped_events)} (removed {len(events) - len(deduped_events)} recurring instances)")
        events = deduped_events

        # Track current Google event UIDs (use plain event ID, not iCalUID)
        # We use event['id'] to match what we store in sync_state
        current_google_ids = {event['id'] for event in events}
        logger.debug(f"Current Google event IDs: {len(current_google_ids)} unique events")

        # Get existing iCloud events to check for duplicates
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
                            # For recurring event instances, create unique ID with recurrence-id or dtstart
                            recurrence_id = component.get('recurrence-id')
                            uid = str(component.get('uid'))
                            if recurrence_id:
                                # This is a specific instance of a recurring event
                                recurrence_str = recurrence_id.dt.isoformat() if hasattr(recurrence_id.dt, 'isoformat') else str(recurrence_id.dt)
                                event_id = f"{uid}_{recurrence_str}"
                            else:
                                # Single event or master recurring event
                                event_id = uid
                            existing_icloud_events[event_id] = icloud_event
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Could not fetch existing iCloud events: {e}")

        # Add new events
        logger.debug(f"Processing {len(events)} Google events...")
        for event in events:
            event_uid = event['id']
            event_title = event.get('summary', 'No Title')
            # iCloud rejects UIDs starting with '_' (Google-internal format); strip them.
            # iCloud also rejects UIDs longer than ~255 chars; hash excessively long UIDs.
            safe_uid = event_uid.lstrip('_')
            if len(safe_uid) > 200:
                safe_uid = hashlib.sha256(safe_uid.encode()).hexdigest()
                logger.debug(f"UID too long, hashed to: {safe_uid}")
            logger.debug(f"Checking event: {event_title} (ID: {event_uid}, iCalUID: {event.get('iCalUID')})")

            # Check if event originated from iCloud by looking it up in sync state.
            # Using iCalUID-based heuristics is unreliable: ICS-imported events also have
            # a foreign iCalUID that doesn't match Google's event ID, causing false positives.
            ical_uid = event.get('iCalUID', '')
            is_icloud_origin = (
                ical_uid and self.state['synced_events'].get(ical_uid, {}).get('source') == 'icloud'
            )
            if is_icloud_origin:
                logger.debug(f"  Skipping: Event originated from iCloud (found in sync state as icloud source)")
                continue

            if event_uid in self.state['synced_events'] and not self.state['synced_events'][event_uid].get('sync_failed'):
                # Event already successfully synced — skip unless Google's timestamp changed
                last_modified = event.get('updated')
                stored_state = self.state['synced_events'][event_uid]
                stored_modified = stored_state.get('last_modified_google') or stored_state.get('last_modified') or stored_state.get('synced_at')
                logger.debug(f"  Event already synced. Checking for modifications...")
                logger.debug(f"    Last modified in Google: {last_modified}")
                logger.debug(f"    Last modified in state (google): {stored_modified}")
                if last_modified and stored_modified and last_modified != stored_modified:
                    logger.debug(f"  Event modified: {event_title} (was: {stored_modified}, now: {last_modified})")
                    # Update the existing iCloud event
                    if safe_uid in existing_icloud_events:
                        try:
                            icloud_event = existing_icloud_events[safe_uid]
                            cal = Calendar.from_ical(icloud_event.data)
                            for component in cal.walk():
                                if component.name == "VEVENT":
                                    component.pop('summary', None)
                                    component.add('summary', event.get('summary', 'No Title'))
                                    if event.get('description'):
                                        component.pop('description', None)
                                        component.add('description', event['description'])
                                    elif 'description' in component:
                                        del component['description']
                                    start = event['start'].get('dateTime', event['start'].get('date'))
                                    end = event['end'].get('dateTime', event['end'].get('date'))
                                    start_dt = datetime.fromisoformat(start.replace('Z', '+00:00'))
                                    end_dt = datetime.fromisoformat(end.replace('Z', '+00:00'))
                                    component.pop('dtstart', None)
                                    component.pop('dtend', None)
                                    component.add('dtstart', start_dt)
                                    component.add('dtend', end_dt)
                                    break
                            icloud_event.data = cal.to_ical()
                            icloud_event.save()
                            self.state['synced_events'][event_uid]['last_modified_google'] = last_modified
                            self.state['synced_events'][event_uid]['last_modified'] = last_modified
                            self.state['synced_events'][event_uid]['title'] = event.get('summary')
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
                self._record_synced_event(
                    event_uid, event.get('summary'),
                    source='google',
                    start=event_start,
                    icloud_uid=safe_uid,
                    last_modified_google=event.get('updated')
                )
                logger.debug(f"Skipped (already exists in iCloud): {event.get('summary')}")
                continue

            # Create iCloud event
            cal = Calendar()
            ical_event = Event()

            ical_event.add('summary', event.get('summary', 'No Title'))
            if event.get('description'):
                ical_event.add('description', event['description'])

            start = event['start'].get('dateTime', event['start'].get('date'))
            end = event['end'].get('dateTime', event['end'].get('date'))

            # Parse datetime and convert to UTC to avoid timezone issues with iCloud
            start_dt = datetime.fromisoformat(start.replace('Z', '+00:00'))
            end_dt = datetime.fromisoformat(end.replace('Z', '+00:00'))

            # Convert to UTC if it has timezone info, otherwise treat as-is
            if hasattr(start_dt, 'tzinfo') and start_dt.tzinfo is not None:
                start_dt = start_dt.astimezone(datetime.now().astimezone().tzinfo.utc)
                end_dt = end_dt.astimezone(datetime.now().astimezone().tzinfo.utc)

            ical_event.add('dtstart', start_dt)
            ical_event.add('dtend', end_dt)
            ical_event.add('uid', safe_uid)

            # Add 30-minute reminder
            alarm = Alarm()
            alarm.add('action', 'DISPLAY')
            alarm.add('trigger', timedelta(minutes=-30))
            alarm.add('description', 'Reminder')
            ical_event.add_component(alarm)

            cal.add_component(ical_event)

            try:
                event_start = event['start'].get('dateTime', event['start'].get('date'))
                ical_data = cal.to_ical()
                retry(lambda: icloud_calendar.save_event(ical_data))
                self._record_synced_event(
                    event_uid, event.get('summary'), source='google',
                    start=event_start, last_modified=event.get('updated'),
                    icloud_uid=safe_uid, last_modified_google=event.get('updated')
                )
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
                    start=event_start, last_modified=event.get('updated'),
                    failed=True, error=str(e)
                )
                error_count += 1

        # Detect deletions: events that were synced from Google but no longer exist
        # Only check events that fall within the current time window
        events_to_delete = []
        logger.debug(f"Checking for deleted Google events. Synced events count: {len(self.state['synced_events'])}")
        for event_id, event_info in self.state['synced_events'].items():
            if event_info.get('source') == 'google':
                # Check if event is within the time window
                event_start = event_info.get('start')
                logger.debug(f"Checking synced event: {event_info.get('title')} (ID: {event_id}, start: {event_start})")
                if event_start:
                    try:
                        # Parse the event start time
                        event_dt = datetime.fromisoformat(event_start.replace('Z', '+00:00'))
                        # Only consider for deletion if within our query window
                        if time_min <= event_dt.isoformat() <= time_max:
                            if event_id not in current_google_ids:
                                logger.debug(f"  → Marked for deletion: not in current Google events")
                                events_to_delete.append(event_id)
                            else:
                                logger.debug(f"  → Still exists in Google")
                        else:
                            logger.debug(f"  → Outside time window (event: {event_dt.isoformat()}, window: {time_min} to {time_max})")
                    except Exception as e:
                        # If we can't parse the date, skip this event
                        logger.debug(f"  → Could not parse date: {e}")

        # Delete events from iCloud that were deleted from Google
        logger.debug(f"Events to delete from iCloud: {len(events_to_delete)}")
        for event_id in events_to_delete:
            logger.debug(f"Attempting to delete event from iCloud: {event_id}")
            try:
                # Find and delete the event in iCloud by UID
                with SuppressCaldavOutput():
                    icloud_events = list(icloud_calendar.date_search(
                        start=now - timedelta(days=1),
                        end=now + timedelta(days=360),
                        expand=True
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
                                safe_event_id = event_id.lstrip('_')
                                logger.debug(f"    Comparing: iCloud UID={full_uid} vs target={safe_event_id}")
                                if full_uid == safe_event_id or icloud_uid == safe_event_id:
                                    logger.debug(f"    → Match found! Deleting...")
                                    icloud_event.delete()
                                    deleted_count += 1
                                    event_info = self.state['synced_events'][event_id]
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
                'deleted_events': deleted_events, 'errors': error_count,
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

                # Already mirrored: update only if Google's timestamp changed
                if state_key in self.state['synced_events'] and not self.state['synced_events'][state_key].get('sync_failed'):
                    last_modified = event.get('updated')
                    stored_state = self.state['synced_events'][state_key]
                    stored_modified = stored_state.get('last_modified_google') or stored_state.get('last_modified') or stored_state.get('synced_at')
                    if last_modified and stored_modified and last_modified != stored_modified and safe_uid in existing_icloud_events:
                        try:
                            icloud_event = existing_icloud_events[safe_uid]
                            cal = Calendar.from_ical(icloud_event.data)
                            for component in cal.walk():
                                if component.name == "VEVENT":
                                    component.pop('summary', None)
                                    component.add('summary', prefixed_title)
                                    if event.get('description'):
                                        component.pop('description', None)
                                        component.add('description', event['description'])
                                    elif 'description' in component:
                                        del component['description']
                                    start = event['start'].get('dateTime', event['start'].get('date'))
                                    end = event['end'].get('dateTime', event['end'].get('date'))
                                    start_dt = datetime.fromisoformat(start.replace('Z', '+00:00'))
                                    end_dt = datetime.fromisoformat(end.replace('Z', '+00:00'))
                                    component.pop('dtstart', None)
                                    component.pop('dtend', None)
                                    component.add('dtstart', start_dt)
                                    component.add('dtend', end_dt)
                                    break
                            icloud_event.data = cal.to_ical()
                            icloud_event.save()
                            self.state['synced_events'][state_key]['last_modified_google'] = last_modified
                            self.state['synced_events'][state_key]['last_modified'] = last_modified
                            self.state['synced_events'][state_key]['title'] = prefixed_title
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
                        start=event_start, icloud_uid=safe_uid,
                        last_modified_google=event.get('updated'),
                        source_calendar_id=calendar_id
                    )
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
                        start=event_start, last_modified=event.get('updated'),
                        icloud_uid=safe_uid, last_modified_google=event.get('updated'),
                        source_calendar_id=calendar_id
                    )
                    synced_count += 1
                    added_events.append({'title': prefixed_title, 'start': event_start})
                    logger.info(f"  Added to iCloud (mirror): {prefixed_title}")
                except Exception as e:
                    logger.error(f"  Failed to mirror '{prefixed_title}': {e}")
                    self._record_synced_event(
                        state_key, prefixed_title, source='google_oneway',
                        start=event_start, last_modified=event.get('updated'),
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
            'errors': error_count
        }

    def sync_icloud_to_google(self, google_service, icloud_calendar):
        """Sync events from iCloud to Google Calendar"""
        logger.info("← Syncing iCloud → Google...")

        now = datetime.now(timezone.utc)
        start = now - timedelta(days=1)
        end = now + timedelta(days=360)

        with SuppressCaldavOutput():
            events = icloud_calendar.date_search(start=start, end=end, expand=True)
        synced_count = 0
        updated_count = 0
        deleted_count = 0
        error_count = 0
        added_events = []
        updated_events = []
        deleted_events = []

        # Track current iCloud event UIDs
        current_icloud_ids = set()

        # Build an index of iCloud UIDs that were written by us from Google-origin events
        # (including hashed UIDs). Used to prevent iCloud→Google ping-pong.
        google_origin_icloud_uids = {
            entry['icloud_uid']
            for entry in self.state['synced_events'].values()
            if entry.get('source') in ('google', 'google_oneway') and entry.get('icloud_uid')
        }

        # Get existing Google events to check for duplicates
        existing_google_events = {}
        try:
            time_min = start.isoformat().replace('+00:00', 'Z')
            time_max = end.isoformat().replace('+00:00', 'Z')

            logger.debug(f"Querying Google Calendar: {self.config['google_calendar_id']}")
            logger.debug(f"Time range: {time_min} to {time_max}")

            page_token = None
            all_google_events = []
            while True:
                google_events_result = google_service.events().list(
                    calendarId=self.config['google_calendar_id'],
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy='startTime',
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

                    # For recurring event instances, create unique ID with recurrence-id or dtstart
                    recurrence_id = component.get('recurrence-id')
                    if recurrence_id:
                        recurrence_str = recurrence_id.dt.isoformat() if hasattr(recurrence_id.dt, 'isoformat') else str(recurrence_id.dt)
                        event_id = f"{uid}_{recurrence_str}"
                    else:
                        event_id = uid

                    current_icloud_ids.add(event_id)

                    # Get iCloud last-modified timestamp
                    last_modified_prop = component.get('last-modified')
                    last_modified = last_modified_prop.dt.isoformat() if last_modified_prop and hasattr(last_modified_prop.dt, 'isoformat') else None

                    if event_id in self.state['synced_events']:
                        # Check if modified since last sync — compare against iCloud timestamp only
                        stored_state = self.state['synced_events'][event_id]
                        stored_modified = stored_state.get('last_modified_icloud') or stored_state.get('last_modified') or stored_state.get('synced_at')
                        if last_modified and stored_modified and last_modified != stored_modified:
                            event_title = str(component.get('summary', 'No Title')) + (" (recurring)" if recurrence_id else "")
                            logger.debug(f"  Event modified: {event_title} (was: {stored_modified}, now: {last_modified})")
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
                                    updated_g_event = google_service.events().patch(
                                        calendarId=self.config['google_calendar_id'],
                                        eventId=g_event['id'],
                                        body=patch_body
                                    ).execute()
                                    self.state['synced_events'][event_id]['last_modified_icloud'] = last_modified
                                    self.state['synced_events'][event_id]['last_modified'] = last_modified
                                    self.state['synced_events'][event_id]['last_modified_google'] = updated_g_event.get('updated')
                                    self.state['synced_events'][event_id]['title'] = event_title
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
                    if event_id in google_origin_icloud_uids:
                        logger.debug(f"  Skipping: iCloud event with hashed UID originated from Google: {component.get('summary')}")
                        continue

                    # Check if event already exists in Google (by UID)
                    if event_id in existing_google_events:
                        # Event already exists in Google, just record it in state
                        dtstart = component.get('dtstart').dt
                        event_start = dtstart.isoformat() if isinstance(dtstart, datetime) else str(dtstart)
                        self._record_synced_event(
                            event_id,
                            str(component.get('summary')) + (" (recurring)" if recurrence_id else ""),
                            source='icloud', start=event_start, last_modified=last_modified,
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

                    event_start = dtstart.isoformat() if isinstance(dtstart, datetime) else str(dtstart)
                    event_title = str(component.get('summary')) + (" (recurring)" if recurrence_id else "")
                    logger.debug(f"Adding event to Google: {google_event['summary']} ({start_dict})")

                    try:
                        retry(lambda: google_service.events().insert(
                            calendarId=self.config['google_calendar_id'],
                            body=google_event
                        ).execute())

                        self._record_synced_event(
                            event_id, event_title, source='icloud',
                            start=event_start, last_modified=last_modified,
                            last_modified_icloud=last_modified
                        )
                        synced_count += 1 if not recurrence_id else 0  # Count only master events
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
                                        singleEvents=True,
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
                                start=event_start, last_modified=last_modified
                            )
                        else:
                            logger.error(f"Failed to add event to Google: {e}")
                            self._record_synced_event(
                                event_id, event_title, source='icloud',
                                start=event_start, last_modified=last_modified,
                                failed=True, error=str(e)
                            )
                            error_count += 1

        # Detect deletions: events that were synced from iCloud but no longer exist
        # Only check events that fall within the current time window
        events_to_delete = []
        logger.debug(f"Checking for deleted iCloud events. Synced events count: {len(self.state['synced_events'])}")
        logger.debug(f"Current iCloud IDs: {current_icloud_ids}")
        for event_id, event_info in self.state['synced_events'].items():
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
        if deleted_count > 0:
            logger.info(f"Deleted {deleted_count} event(s) from Google")

        return {
            'added': synced_count,
            'updated': updated_count,
            'deleted': deleted_count,
            'added_events': added_events,
            'updated_events': updated_events,
            'deleted_events': deleted_events,
            'errors': error_count
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

            notification_text = "Calendar Reconcile", "\n".join(lines)
            logger.debug("Reconcile results:\n" + "\n".join(lines))

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
