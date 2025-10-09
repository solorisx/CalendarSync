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
from datetime import datetime, timedelta
from pathlib import Path
from icalendar import Calendar, Event
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

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
        # print(f"Looking for iCloud calendar: {calendar_name}")
        # print(f"iCloud calendars found: {[cal.name for cal in calendars]}")
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
        added_events = []
        deleted_events = []

        # Track current Google event IDs
        current_google_ids = {event['id'] for event in events}

        # Add new events
        for event in events:
            # Use the actual event ID without prefix to avoid duplicates
            event_id = event['id']

            if event_id in self.state['synced_events']:
                continue

            # Create iCloud event
            cal = Calendar()
            ical_event = Event()

            ical_event.add('summary', event.get('summary', 'No Title'))
            if event.get('description'):
                ical_event.add('description', event['description'])

            start = event['start'].get('dateTime', event['start'].get('date'))
            end = event['end'].get('dateTime', event['end'].get('date'))

            ical_event.add('dtstart', datetime.fromisoformat(start.replace('Z', '+00:00')))
            ical_event.add('dtend', datetime.fromisoformat(end.replace('Z', '+00:00')))
            ical_event.add('uid', event['id'])

            cal.add_component(ical_event)

            try:
                icloud_calendar.save_event(cal.to_ical())
                event_start = event['start'].get('dateTime', event['start'].get('date'))
                self.state['synced_events'][event_id] = {
                    'title': event.get('summary'),
                    'synced_at': datetime.now().isoformat(),
                    'source': 'google',
                    'start': event_start
                }
                synced_count += 1
                added_events.append({
                    'title': event.get('summary'),
                    'start': event_start
                })
                print(f"  ✓ {event.get('summary')}")
            except Exception as e:
                print(f"  ✗ Error: {e}")

        # Detect deletions: events that were synced from Google but no longer exist
        events_to_delete = []
        for event_id, event_info in self.state['synced_events'].items():
            if event_info.get('source') == 'google' and event_id not in current_google_ids:
                events_to_delete.append(event_id)

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
            'deleted_events': deleted_events
        }

    def sync_icloud_to_google(self, google_service, icloud_calendar):
        """Sync events from iCloud to Google Calendar"""
        print("← Syncing iCloud → Google...")

        now = datetime.now()
        start = now - timedelta(days=1)
        end = now + timedelta(days=90)

        events = icloud_calendar.date_search(start=start, end=end)
        synced_count = 0
        deleted_count = 0
        added_events = []
        deleted_events = []

        # Track current iCloud event UIDs
        current_icloud_ids = set()

        # Add new events
        for event in events:
            try:
                ical = Calendar.from_ical(event.data)
            except Exception as e:
                print(f"  ✗ Error parsing event: {e}")
                continue

            for component in ical.walk():
                if component.name == "VEVENT":
                    # Use the actual UID without prefix to avoid duplicates
                    uid = str(component.get('uid'))
                    event_id = uid
                    current_icloud_ids.add(event_id)

                    if event_id in self.state['synced_events']:
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
                    }

                    if component.get('description'):
                        google_event['description'] = str(component.get('description'))

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
                        print(f"  ✓ {component.get('summary')}")
                    except Exception as e:
                        print(f"  ✗ Error: {e}")

        # Detect deletions: events that were synced from iCloud but no longer exist
        events_to_delete = []
        for event_id, event_info in self.state['synced_events'].items():
            if event_info.get('source') == 'icloud' and event_id not in current_icloud_ids:
                events_to_delete.append(event_id)

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

            message = f"✓ Sync complete: {google_result['added']} from Google, {icloud_result['added']} from iCloud"
            print(f"\n{message}\n")

            # Send notification if any events were added or deleted
            if total_added > 0 or total_deleted > 0:
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

                notification_message = "\n".join(notification_parts)
                self.send_notification("Calendar Sync", notification_message)

            return True

        except Exception as e:
            error_msg = f"✗ Sync failed: {str(e)}"
            print(f"\n{error_msg}\n")

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
