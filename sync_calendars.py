#!/usr/bin/env python3
"""
Bidirectional Calendar Sync: Google Calendar <-> iCloud
With notify.sh notifications
"""

import os
import sys
import json
import pickle
import caldav
import requests
import time
import logging
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from icalendar import Calendar, Event
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

if False:
    # Enable detailed HTTP logging for caldav
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    logging.getLogger('caldav').setLevel(logging.DEBUG)
    # Also enable urllib3 logging to see raw HTTP requests and request bodies
    logging.getLogger('urllib3').setLevel(logging.DEBUG)
    # Enable HTTP request/response body logging
    import http.client
    http.client.HTTPConnection.debuglevel = 1

# Configuration
CONFIG_FILE = '/app/data/config.json'
TOKEN_FILE = '/app/data/token.pickle'
STATE_FILE = '/app/data/sync_state.json'
CREDENTIALS_FILE = '/app/data/credentials.json'

SCOPES = ['https://www.googleapis.com/auth/calendar']
SYNC_INTERVAL = int(os.getenv('SYNC_INTERVAL', '900'))  # 15 minutes default

class CalendarSync:
    def __init__(self):
        self.config = self.load_config()
        self.state = self.load_state()

    def load_config(self):
        """Load configuration from file"""
        if not os.path.exists(CONFIG_FILE):
            print(f"Error: {CONFIG_FILE} not found. Please create it.")
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
        """Send notification to notify.sh"""
        notify_url = self.config.get('notify_url')
        if not notify_url:
            print(f"Notification: {title} - {message}")
            return

        try:
            response = requests.post(notify_url, data=message.encode('utf-8'))
            if response.status_code == 200:
                print(f"✓ Notification sent: {title}")
            else:
                print(f"✗ Failed to send notification: {response.status_code}")
        except Exception as e:
            print(f"✗ Error sending notification: {e}")

    def get_google_service(self):
        """Authenticate and return Google Calendar service"""
        creds = None

        if os.path.exists(TOKEN_FILE):
            with open(TOKEN_FILE, 'rb') as token:
                creds = pickle.load(token)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(CREDENTIALS_FILE):
                    print(f"Error: {CREDENTIALS_FILE} not found")
                    print("Download from Google Cloud Console")
                    sys.exit(1)
                flow = InstalledAppFlow.from_client_secrets_file(
                    CREDENTIALS_FILE, SCOPES)
                # Use port 8080 for Docker compatibility
                creds = flow.run_local_server(port=8080, host='0.0.0.0')

            with open(TOKEN_FILE, 'wb') as token:
                pickle.dump(creds, token)

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
        print("→ Syncing Google → iCloud...")

        now = datetime.utcnow()
        time_min = (now - timedelta(days=1)).isoformat() + 'Z'
        time_max = (now + timedelta(days=90)).isoformat() + 'Z'

        events_result = google_service.events().list(
            calendarId=self.config['google_calendar_id'],
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy='startTime'
        ).execute()

        events = events_result.get('items', [])
        synced_count = 0
        deleted_count = 0
        error_count = 0
        added_events = []
        deleted_events = []

        # Track current Google event UIDs (using iCalUID if available, otherwise event ID)
        current_google_ids = {event.get('iCalUID', event['id']) for event in events}

        # Get existing iCloud events to check for duplicates
        existing_icloud_events = {}
        try:
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
            print(f"  ⚠ Warning: Could not fetch existing iCloud events: {e}")

        # Add new events
        for event in events:

            if event.get('iCalUID'):
                # source is iCloud
                continue

            event_uid = event['id']

            if event_uid in self.state['synced_events']:
                # already synced
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
                print(f"  ⊘ {event.get('summary')} (already exists in iCloud)")
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
                print(f"  ✓ {event.get('summary')}")
            except Exception as e:
                print(f"  ✗ Error syncing '{event.get('summary')}': {e}")
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
                                    print(f"  ✗ Deleted: {event_info['title']}")
                                    del self.state['synced_events'][event_id]
                                    break
                    except:
                        pass
            except Exception as e:
                print(f"  ✗ Error deleting event: {e}")

        if deleted_count > 0:
            print(f"  Deleted {deleted_count} event(s) from iCloud")

        return {
            'added': synced_count,
            'deleted': deleted_count,
            'added_events': added_events,
            'deleted_events': deleted_events,
            'errors': error_count
        }

    def sync_icloud_to_google(self, google_service, icloud_calendar):
        """Sync events from iCloud to Google Calendar"""
        print("← Syncing iCloud → Google...")

        now = datetime.now()
        start = now - timedelta(days=1)
        end = now + timedelta(days=90)

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
            print(f"  ⚠ Warning: Could not fetch existing Google events: {e}")

        # Add new events
        print(f"  Processing {len(events)} iCloud events...")
        for event in events:
            try:
                ical = Calendar.from_ical(event.data)
            except Exception as e:
                print(f"  ✗ Error parsing event: {e}")
                continue

            for component in ical.walk():
                if component.name == "VEVENT":
                    uid = str(component.get('uid'))

                    # For recurring event instances, create unique ID with recurrence-id or dtstart
                    recurrence_id = component.get('recurrence-id')
                    if recurrence_id:
                        # This is a specific instance of a recurring event
                        recurrence_str = recurrence_id.dt.isoformat() if hasattr(recurrence_id.dt, 'isoformat') else str(recurrence_id.dt)
                        event_id = f"{uid}_{recurrence_str}"
                    else:
                        # Single event or master recurring event
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
                        print(f"  ⊘ {component.get('summary')} (already exists in Google)")
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
                    }

                    if component.get('description'):
                        google_event['description'] = str(component.get('description'))

                    print(f"  Adding event to Google: {google_event['summary']} ({start_dict})")

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
                    except Exception as e:
                        print(f"  ✗ Error: {e}")
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
                print(f"  ✗ Deleted: {event_info['title']}")
                del self.state['synced_events'][event_id]
            except Exception as e:
                # Event might already be deleted or not found
                if event_id in self.state['synced_events']:
                    del self.state['synced_events'][event_id]

        if deleted_count > 0:
            print(f"  Deleted {deleted_count} event(s) from Google")

        return {
            'added': synced_count,
            'deleted': deleted_count,
            'added_events': added_events,
            'deleted_events': deleted_events
        }

    def run_sync(self):
        """Execute bidirectional sync"""
        try:
            print(f"\n{'='*60}")
            print(f"Starting sync at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"{'='*60}")

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

            message = f"✓ Sync complete: {google_result['added']} from Google, {icloud_result['added']} from iCloud"
            print(f"\n{message}\n")

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
            error_msg = f"✗ Sync failed: {str(e)}"
            print(f"\n{error_msg}\n")

            traceback.print_exc()

            # Only send notification if this is a new/different error
            last_error = self.state.get('last_error')
            if last_error != error_msg:
                self.send_notification("Calendar Sync Error", error_msg)
                self.state['last_error'] = error_msg
                self.save_state()

            return False

def main():
    """Main loop with scheduled syncing"""
    print("Calendar Sync Service Starting...")
    print(f"Sync interval: {SYNC_INTERVAL} seconds")

    sync = CalendarSync()

    while True:
        sync.run_sync()
        print(f"Next sync in {SYNC_INTERVAL} seconds...")
        time.sleep(SYNC_INTERVAL)

if __name__ == "__main__":
    main()
