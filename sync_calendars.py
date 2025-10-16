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
import warnings
from contextlib import redirect_stderr
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
        return {'last_sync': None, 'synced_events': {}, 'last_error': None}

    def save_state(self):
        """Save sync state"""
        with open(STATE_FILE, 'w') as f:
            json.dump(self.state, f, indent=2)

    def send_notification(self, title, message):
        """Send notification to ntfy.sh"""
        notify_url = self.config.get('notify_url')
        if not notify_url:
            logger.info(f"Notification: {title} - {message}")
            return

        try:
            response = requests.post(notify_url, data=message.encode('utf-8'))
            if response.status_code == 200:
                logger.info(f"Notification sent: {title}")
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
        time_max = (now + timedelta(days=90)).isoformat().replace('+00:00', 'Z')

        logger.debug(f"Querying Google Calendar: {self.config['google_calendar_id']}")
        logger.debug(f"Time range: {time_min} to {time_max}")

        events_result = google_service.events().list(
            calendarId=self.config['google_calendar_id'],
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy='startTime'
        ).execute()

        logger.debug(f"Google API response keys: {events_result.keys()}")
        logger.info(f"Fetched {events_result.get('totalItems', 0)} events from Google Calendar")

        events = events_result.get('items', [])
        logger.debug(f"Number of events in 'items': {len(events)}")
        synced_count = 0
        deleted_count = 0
        error_count = 0
        added_events = []
        deleted_events = []

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
                    end=now + timedelta(days=90),
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
                except:
                    pass
        except Exception as e:
            logger.warning(f"Could not fetch existing iCloud events: {e}")

        # Add new events
        logger.debug(f"Processing {len(events)} Google events...")
        for event in events:
            event_uid = event['id']
            event_title = event.get('summary', 'No Title')
            logger.debug(f"Checking event: {event_title} (ID: {event_uid}, iCalUID: {event.get('iCalUID')})")

            # Check if event originated from iCloud by comparing iCalUID with event ID
            # Google native events have iCalUID that matches their ID (often with @google.com)
            # iCloud synced events have iCalUID that differs from the Google event ID
            ical_uid = event.get('iCalUID', '')
            if ical_uid and not ical_uid.startswith(event_uid):
                # source is iCloud (iCalUID is different from Google's event ID)
                logger.debug(f"  Skipping: Event originated from iCloud (iCalUID doesn't match event ID)")
                continue

            if event_uid in self.state['synced_events']:
                # already synced
                logger.debug(f"  Skipping: Already in sync state")
                continue

            # Check if event already exists in iCloud (by UID)
            if event_uid in existing_icloud_events:
                # Event already exists in iCloud, just record it in state
                event_start = event['start'].get('dateTime', event['start'].get('date'))
                self.state['synced_events'][event_uid] = {
                    'title': event.get('summary'),
                    'synced_at': datetime.now().isoformat(),
                    'source': 'icloud' if event.get('iCalUID') else 'google',
                    'start': event_start
                }
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
            ical_event.add('uid', event_uid)

            # Add 30-minute reminder
            alarm = Alarm()
            alarm.add('action', 'DISPLAY')
            alarm.add('trigger', timedelta(minutes=-30))
            alarm.add('description', 'Reminder')
            ical_event.add_component(alarm)

            cal.add_component(ical_event)

            try:
                event_start = event['start'].get('dateTime', event['start'].get('date'))
                icloud_calendar.save_event(cal.to_ical())
                self.state['synced_events'][event_uid] = {
                    'title': event.get('summary'),
                    'synced_at': datetime.now().isoformat(),
                    'source': 'google',
                    'start': event_start,
                }
                synced_count += 1
                added_events.append({
                    'title': event.get('summary'),
                    'start': event_start
                })
                logger.info(f"Added to iCloud: {event.get('summary')}")
            except Exception as e:
                logger.error(f"Failed to sync '{event.get('summary')}': {e}")
                # Still mark as synced to avoid retrying on every sync
                self.state['synced_events'][event_uid] = {
                    'title': event.get('summary'),
                    'synced_at': datetime.now().isoformat(),
                    'source': 'google',
                    'start': event_start,
                    'sync_failed': True,
                    'error': str(e),
                }
                error_count += 1

        # Detect deletions: events that were synced from Google but no longer exist
        # Only check events that fall within the current time window
        events_to_delete = []
        for event_id, event_info in self.state['synced_events'].items():
            if event_info.get('source') == 'google':
                # Check if event is within the time window
                event_start = event_info.get('start')
                if event_start:
                    try:
                        # Parse the event start time
                        event_dt = datetime.fromisoformat(event_start.replace('Z', '+00:00'))
                        # Only consider for deletion if within our query window
                        if time_min <= event_dt.isoformat() <= time_max:
                            if event_id not in current_google_ids:
                                events_to_delete.append(event_id)
                    except:
                        # If we can't parse the date, skip this event
                        pass

        # Delete events from iCloud that were deleted from Google
        for event_id in events_to_delete:
            try:
                # Find and delete the event in iCloud by UID
                icloud_events = icloud_calendar.date_search(
                    start=now - timedelta(days=365),
                    end=now + timedelta(days=365)
                )
                for icloud_event in icloud_events:
                    try:
                        ical = Calendar.from_ical(icloud_event.data)
                        for component in ical.walk():
                            if component.name == "VEVENT":
                                if str(component.get('uid')) == event_id:
                                    icloud_event.delete()
                                    deleted_count += 1
                                    event_info = self.state['synced_events'][event_id]
                                    deleted_events.append({
                                        'title': event_info['title'],
                                        'start': event_info.get('start', 'unknown')
                                    })
                                    logger.info(f"Deleted from iCloud: {event_info['title']}")
                                    del self.state['synced_events'][event_id]
                                    break
                    except:
                        pass
            except Exception as e:
                logger.error(f"Error deleting event: {e}")

        if deleted_count > 0:
            logger.info(f"Deleted {deleted_count} event(s) from iCloud")

        return {
            'added': synced_count,
            'deleted': deleted_count,
            'added_events': added_events,
            'deleted_events': deleted_events,
            'errors': error_count
        }

    def sync_icloud_to_google(self, google_service, icloud_calendar):
        """Sync events from iCloud to Google Calendar"""
        logger.info("← Syncing iCloud → Google...")

        now = datetime.now()
        start = now - timedelta(days=1)
        end = now + timedelta(days=90)

        with SuppressCaldavOutput():
            events = icloud_calendar.date_search(start=start, end=end, expand=True)
        synced_count = 0
        deleted_count = 0
        error_count = 0
        added_events = []
        deleted_events = []

        # Track current iCloud event UIDs
        current_icloud_ids = set()

        # Get existing Google events to check for duplicates
        existing_google_events = {}
        try:
            time_min = (now - timedelta(days=1)).isoformat() + 'Z'
            time_max = (now + timedelta(days=90)).isoformat() + 'Z'
            google_events_result = google_service.events().list(
                calendarId=self.config['google_calendar_id'],
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True
            ).execute()
            for g_event in google_events_result.get('items', []):
                # Use iCalUID if available (for events synced from iCloud), otherwise use event ID
                # iCalUID will be in format: "uid" or "uid_recurrence-datetime" for recurring instances
                event_uid = g_event.get('iCalUID', g_event['id'])
                existing_google_events[event_uid] = g_event
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

                    if event_id in self.state['synced_events']:
                        continue

                    # Check if event already exists in Google (by UID)
                    if event_id in existing_google_events:
                        # Event already exists in Google, just record it in state
                        dtstart = component.get('dtstart').dt
                        event_start = dtstart.isoformat() if isinstance(dtstart, datetime) else str(dtstart)
                        self.state['synced_events'][event_id] = {
                            'title': str(component.get('summary')),
                            'synced_at': datetime.now().isoformat(),
                            'source': 'icloud',
                            'start': event_start
                        }
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

                    logger.debug(f"Adding event to Google: {google_event['summary']} ({start_dict})")

                    try:
                        google_service.events().insert(
                            calendarId=self.config['google_calendar_id'],
                            body=google_event
                        ).execute()

                        event_start = dtstart.isoformat() if isinstance(dtstart, datetime) else str(dtstart)
                        self.state['synced_events'][event_id] = {
                            'title': str(component.get('summary')),
                            'synced_at': datetime.now().isoformat(),
                            'source': 'icloud',
                            'start': event_start
                        }
                        synced_count += 1
                        added_events.append({
                            'title': str(component.get('summary')),
                            'start': event_start
                        })
                        logger.info(f"Added to Google: {component.get('summary')}")
                    except Exception as e:
                        logger.error(f"Failed to add event to Google: {e}")
                        self.state['synced_events'][event_id] = {
                            'title': str(component.get('summary')),
                            'synced_at': datetime.now().isoformat(),
                            'source': 'icloud',
                            'start': event_start,
                            'sync_failed': True,
                            'error': str(e),
                        }
                        error_count += 1

        # Detect deletions: events that were synced from iCloud but no longer exist
        # Only check events that fall within the current time window
        events_to_delete = []
        for event_id, event_info in self.state['synced_events'].items():
            if event_info.get('source') == 'icloud':
                # Check if event is within the time window
                event_start = event_info.get('start')
                if event_start:
                    try:
                        # Parse the event start time
                        event_dt = datetime.fromisoformat(event_start.replace('Z', '+00:00'))
                        # Only consider for deletion if within our query window
                        if start <= event_dt <= end:
                            if event_id not in current_icloud_ids:
                                events_to_delete.append(event_id)
                    except:
                        # If we can't parse the date, skip this event
                        pass

        # Delete events from Google that were deleted from iCloud
        for event_id in events_to_delete:
            try:
                # Search for the event in Google Calendar by ID
                google_service.events().delete(
                    calendarId=self.config['google_calendar_id'],
                    eventId=event_id
                ).execute()
                deleted_count += 1
                event_info = self.state['synced_events'][event_id]
                deleted_events.append({
                    'title': event_info['title'],
                    'start': event_info.get('start', 'unknown')
                })
                logger.info(f"Deleted from Google: {event_info['title']}")
                del self.state['synced_events'][event_id]
            except Exception as e:
                # Event might already be deleted or not found
                logger.debug(f"Could not delete event {event_id}: {e}")
                if event_id in self.state['synced_events']:
                    del self.state['synced_events'][event_id]

        if deleted_count > 0:
            logger.info(f"Deleted {deleted_count} event(s) from Google")

        return {
            'added': synced_count,
            'deleted': deleted_count,
            'added_events': added_events,
            'deleted_events': deleted_events,
            'errors': error_count
        }

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
            icloud_result = self.sync_icloud_to_google(google_service, icloud_calendar)

            self.state['last_sync'] = datetime.now().isoformat()
            # Clear last error on successful sync
            if self.state.get('last_error'):
                self.state['last_error'] = None
            self.save_state()

            total_added = google_result['added'] + icloud_result['added']
            total_deleted = google_result['deleted'] + icloud_result['deleted']
            total_errors = google_result.get('errors', 0) + icloud_result.get('errors', 0)

            # Calculate elapsed time
            elapsed = time.time() - start_time
            if elapsed < 60:
                time_str = f"{elapsed:.1f}s"
            else:
                minutes = int(elapsed // 60)
                seconds = elapsed % 60
                time_str = f"{minutes}m {seconds:.1f}s"

            message = f"Sync complete: {google_result['added']} from Google, {icloud_result['added']} from iCloud, {google_result['deleted']} deleted from iCloud, {icloud_result['deleted']} deleted from Google"
            message += f", {total_errors} errors occurred in {time_str}"
            logger.info(message)

            # Send notification if any events were added or deleted
            if total_added > 0 or total_deleted > 0 or total_errors > 0:
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

                # Deleted events from Google
                if google_result['deleted'] > 0:
                    notification_parts.append(f"Deleted {google_result['deleted']} from iCloud:")
                    for evt in google_result['deleted_events'][:5]:
                        date_str = evt['start'][:10] if len(str(evt['start'])) > 10 else str(evt['start'])
                        notification_parts.append(f"  - {evt['title']} ({date_str})")
                    if google_result['deleted'] > 5:
                        notification_parts.append(f"  ... and {google_result['deleted'] - 5} more")

                # Deleted events from iCloud
                if icloud_result['deleted'] > 0:
                    notification_parts.append(f"Deleted {icloud_result['deleted']} from Google:")
                    for evt in icloud_result['deleted_events'][:5]:
                        date_str = evt['start'][:10] if len(str(evt['start'])) > 10 else str(evt['start'])
                        notification_parts.append(f"  - {evt['title']} ({date_str})")
                    if icloud_result['deleted'] > 5:
                        notification_parts.append(f"  ... and {icloud_result['deleted'] - 5} more")

                # Include error count if any errors occurred
                if google_result['errors'] > 0:
                    notification_parts.append(f"  ✗ {google_result['errors']} errors occurred from Google")
                if icloud_result['errors'] > 0:
                    notification_parts.append(f"  ✗ {icloud_result['errors']} errors occurred from iCloud")

                notification_message = "\n".join(notification_parts)
                self.send_notification("Calendar Sync", notification_message)

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

    sync = CalendarSync()

    while True:
        sync.run_sync()
        logger.info(f"Next sync in {SYNC_INTERVAL} seconds...")
        time.sleep(SYNC_INTERVAL)

if __name__ == "__main__":
    main()
