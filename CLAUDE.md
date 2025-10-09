# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

CalendarSync2 is a bidirectional calendar synchronization service that runs in Docker, syncing events between Google Calendar and iCloud Calendar. The application runs continuously on a configurable interval (default 15 minutes) and optionally sends notifications via ntfy.sh.

## Architecture

### Core Component
- `sync_calendars.py` - Single-file Python application containing the `CalendarSync` class
  - Handles bidirectional sync logic
  - Manages authentication for both Google (OAuth2) and iCloud (CalDAV)
  - Maintains sync state to avoid duplicate event creation
  - Sends notifications on sync completion or errors

### State Management
The application uses three persistent files in `/app/data/`:
- `config.json` - Calendar credentials and configuration
- `token.pickle` - Google OAuth2 tokens (auto-refreshed)
- `sync_state.json` - Tracks synced events with their UIDs, titles, start dates, and source

### Event ID Strategy
Events are tracked by their actual UID (no prefixes) to prevent duplicates. Each event stores:
- `title` - Event name
- `synced_at` - Timestamp of when it was synced
- `source` - Either "google" or "icloud" to indicate origin
- `start` - Event start date/time

This allows the sync to:
- Detect and skip already-synced events
- Propagate deletions: if an event is deleted from its source, it's removed from the destination
- Avoid ping-pong effect where events get re-synced repeatedly

## Development Commands

### Docker Operations
```bash
# Build and start the service
docker-compose up -d

# View logs (follow mode)
docker-compose logs -f

# Stop the service
docker-compose down

# Rebuild after code changes
docker-compose up -d --build
```

### Initial Setup (First Time Only)
1. Run `./setup.sh` to create data directory and config template
2. Download Google OAuth credentials from Google Cloud Console:
   - Enable Google Calendar API
   - Create OAuth 2.0 Desktop credentials
   - Add yourself as a test user (required!)
   - Save as `data/credentials.json`
3. Edit `data/config.json` with:
   - iCloud CalDAV URL, username (Apple ID), app-specific password
   - Google calendar ID (use "primary" for main calendar)
   - Optional: ntfy.sh URL for notifications
4. Run initial OAuth authentication locally:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   python initial_auth.py
   ```
5. After OAuth completes, start the Docker service:
   ```bash
   docker-compose up -d
   ```

### Testing Locally (Outside Docker)
```bash
# Install dependencies
pip install -r requirements.txt

# Set up data directory
mkdir -p data
cp data/config.json.template data/config.json
# Edit data/config.json with credentials

# Run sync once (modify main() to run once instead of loop)
python sync_calendars.py
```

### Configuration
- `SYNC_INTERVAL` environment variable controls sync frequency (seconds)
- Default: 900 seconds (15 minutes)
- Configure in `docker-compose.yml` environment section

### Manual Sync Commands

**Run an immediate sync (without waiting for interval):**
```bash
# Using the helper script
./sync_now.sh

# Or directly with Docker
docker-compose run --rm calendar-sync python sync_once.py

# Or exec into running container
docker-compose exec calendar-sync python sync_once.py
```

## Code Structure Notes

### CalendarSync Class Methods
- `get_google_service()` - Handles OAuth2 flow and token refresh (sync_calendars.py:~72-96)
- `get_icloud_calendar()` - Connects via CalDAV protocol, validates calendar name (sync_calendars.py:~98-123)
- `sync_google_to_icloud()` - Fetches Google events, detects new/deleted events, syncs to iCloud (sync_calendars.py:~125-234)
  - Returns dict with added/deleted counts and event details
  - Propagates deletions: removes events from iCloud if deleted from Google
- `sync_icloud_to_google()` - Parses iCal events, detects new/deleted events, syncs to Google (sync_calendars.py:~236-354)
  - Returns dict with added/deleted counts and event details
  - Propagates deletions: removes events from Google if deleted from iCloud
- `run_sync()` - Orchestrates bidirectional sync, error handling, and notifications (sync_calendars.py:~356-435)

### Time Ranges
- Both sync directions cover: yesterday to +90 days
- Prevents syncing old historical events unnecessarily

### Error Handling & Notifications
- Individual event failures don't stop the sync process
- Failed events are logged but sync continues
- Error notifications are sent only once per unique error (prevents spam)
- Success notifications are sent only when events are added or deleted
- Notifications include detailed event information:
  - Event titles and dates (up to 5 events per category)
  - Separate sections for added/deleted events from each source
  - Summary counts if more than 5 events

## Dependencies

Key libraries:
- `caldav` - iCloud CalDAV protocol
- `google-api-python-client` - Google Calendar API
- `icalendar` - iCal format parsing/generation
- `requests` - notify.sh HTTP notifications

## Important Notes

### Volume-Mounted Files (No Rebuild Required)
Changes to these files are immediately visible in the container:
- `data/config.json` - Just restart: `docker-compose restart`
- `data/credentials.json`
- `data/token.pickle`
- `data/sync_state.json`

### Code Changes (Rebuild Required)
Rebuild the container when modifying:
- `sync_calendars.py`
- `sync_once.py`
- `requirements.txt`
- `Dockerfile`

Use: `docker-compose build && docker-compose up -d`

## Common Modifications

### Changing Sync Direction
To make sync unidirectional, comment out one of the sync calls in `run_sync()`:
```python
google_result = self.sync_google_to_icloud(google_service, icloud_calendar)
# icloud_result = self.sync_icloud_to_google(google_service, icloud_calendar)  # Disabled
```

### Adjusting Time Window
Modify `timedelta(days=90)` in both sync methods to change how far ahead to sync

### Disabling Deletion Propagation
Comment out the deletion detection sections in both `sync_google_to_icloud()` and `sync_icloud_to_google()`

### Notification Customization
- Modify notification limits (currently 5 events per category) in `run_sync()`
- Change notification triggers (currently: any add/delete)
- Adjust date formatting in the notification message construction
