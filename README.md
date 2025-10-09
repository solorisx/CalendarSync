# CalendarSync2

Bidirectional calendar synchronization between Google Calendar and iCloud Calendar. Runs as a Docker container with automatic sync intervals and optional notifications.

## Features

- ✅ **Bidirectional Sync** - Events sync both ways between Google and iCloud
- 🔄 **Automatic Sync** - Runs continuously on a configurable interval (default: 15 minutes)
- 🗑️ **Deletion Propagation** - Deleting an event in one calendar removes it from the other
- 📱 **Smart Notifications** - Get notified only when events are added/deleted (via ntfy.sh)
- 🐳 **Docker-based** - Easy deployment with Docker Compose
- 💾 **State Tracking** - Prevents duplicate syncing and tracks event sources
- 🔒 **Secure** - OAuth2 for Google, app-specific password for iCloud

## Quick Start

### Prerequisites

- Docker and Docker Compose
- Google Cloud account with Calendar API access
- iCloud account with app-specific password
- (Optional) ntfy.sh channel for notifications

### 1. Setup

```bash
# Clone/download the repository
cd CalendarSync2

# Run setup script
./setup.sh
```

### 2. Configure Google Calendar

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select existing one
3. Enable **Google Calendar API**
4. Go to **OAuth consent screen**:
   - Choose "External" user type
   - Fill in app name and your email
   - Add yourself as a **test user** (important!)
5. Go to **Credentials** → Create **OAuth 2.0 Client ID**:
   - Application type: **Desktop app**
   - Download the JSON file as `data/credentials.json`

### 3. Configure iCloud

1. Go to [appleid.apple.com](https://appleid.apple.com/)
2. Sign in and go to **Security** section
3. Generate an **app-specific password**
4. Save this password for the next step

### 4. Edit Configuration

Edit `data/config.json`:

```json
{
  "google_calendar_id": "primary",
  "icloud": {
    "url": "https://caldav.icloud.com/",
    "username": "your-apple-id@icloud.com",
    "password": "xxxx-xxxx-xxxx-xxxx",
    "calendar_name": "Calendar"
  },
  "notify_url": "https://ntfy.sh/your-unique-channel"
}
```

**Configuration notes:**
- `google_calendar_id`: Use `"primary"` for your main calendar, or specific calendar ID
- `calendar_name`: Name of your iCloud calendar (usually "Calendar", run sync to see available names in error message if wrong)
- `notify_url`: Optional - set to `null` or `""` to disable notifications

### 5. Initial Authentication

Run OAuth authentication locally (Docker can't open a browser):

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run initial authentication
python initial_auth.py
```

A browser window will open. Sign in with your Google account and authorize the app.

### 6. Start the Service

```bash
# Start in background
docker-compose up -d

# View logs
docker-compose logs -f
```

## Usage

### View Logs

```bash
docker-compose logs -f
```

### Manual Sync

Trigger an immediate sync without waiting for the interval:

```bash
./sync_now.sh
```

### Restart Service

```bash
docker-compose restart
```

### Stop Service

```bash
docker-compose down
```

### Change Sync Interval

Edit `docker-compose.yml` and change `SYNC_INTERVAL` (in seconds):

```yaml
environment:
  - SYNC_INTERVAL=300  # 5 minutes
```

Then restart: `docker-compose restart`

## How It Works

### Sync Process

1. **Every 15 minutes** (configurable), the service:
   - Fetches events from Google Calendar (yesterday to +90 days)
   - Fetches events from iCloud Calendar (same range)
   - Compares with previously synced events
   - Adds new events to the opposite calendar
   - Removes events that were deleted from their source

2. **State tracking** prevents duplicates:
   - Each event is tracked by its UID
   - Source is recorded (Google or iCloud)
   - Already-synced events are skipped

3. **Notifications** (if configured):
   - Sent only when events are added or deleted
   - Includes event names and dates
   - Shows up to 5 events per category
   - Errors are reported only once (no spam)

### Deletion Propagation

- Delete an event in Google Calendar → automatically deleted from iCloud
- Delete an event in iCloud Calendar → automatically deleted from Google Calendar
- Only events created by this sync tool are deleted (based on source tracking)

## Notifications

### Setting up ntfy.sh

1. Generate a unique, random channel name:
   ```bash
   echo "https://ntfy.sh/calendar-sync-$(openssl rand -hex 12)"
   ```

2. Add the URL to `data/config.json`

3. Subscribe to notifications:
   - Visit the URL in your browser, OR
   - Install ntfy app on your phone and subscribe to your channel

### Notification Format

```
Calendar Sync

Added 2 from Google:
  + Team Meeting (2025-10-15)
  + Doctor Appointment (2025-10-16)

Deleted 1 from iCloud:
  - Old Event (2025-10-10)
```

## Troubleshooting

### Error: "Access blocked: CalendarSync has not completed Google verification"

**Solution:** Add yourself as a test user in Google Cloud Console:
- OAuth consent screen → Test users → Add your email

### Error: "Google Calendar API has not been used in project..."

**Solution:** Enable the Google Calendar API:
- APIs & Services → Library → Search "Google Calendar API" → Enable

### Error: "iCloud calendar 'X' not found"

**Solution:** Check available calendar names:
1. Run sync once - error message shows available calendars
2. Update `calendar_name` in `data/config.json` with correct name
3. Restart: `docker-compose restart`

### Error: "could not locate runnable browser"

**Solution:** Run initial OAuth locally, not in Docker:
```bash
python initial_auth.py
```

### No events syncing

**Possible causes:**
1. **Time range** - Only syncs events from yesterday to +90 days
2. **Already synced** - Check `data/sync_state.json` to see tracked events
3. **Wrong calendar** - Verify `google_calendar_id` and `calendar_name`

**To reset sync state:**
```bash
echo '{"last_sync": null, "synced_events": {}, "last_error": null}' > data/sync_state.json
docker-compose restart
```

## File Structure

```
CalendarSync2/
├── sync_calendars.py      # Main sync application
├── sync_once.py           # Single sync execution (no loop)
├── initial_auth.py        # OAuth authentication helper
├── Dockerfile             # Container definition
├── docker-compose.yml     # Service configuration
├── requirements.txt       # Python dependencies
├── setup.sh              # Initial setup script
├── sync_now.sh           # Manual sync trigger
├── data/                 # Persistent data (mounted volume)
│   ├── config.json       # Your credentials & settings
│   ├── credentials.json  # Google OAuth credentials
│   ├── token.pickle      # Google OAuth token (auto-generated)
│   └── sync_state.json   # Sync state (auto-generated)
└── README.md            # This file
```

## Advanced Configuration

### Sync Only One Direction

Edit `sync_calendars.py` in the `run_sync()` method and comment out one direction:

```python
google_result = self.sync_google_to_icloud(google_service, icloud_calendar)
# icloud_result = self.sync_icloud_to_google(google_service, icloud_calendar)  # Disabled
```

Then rebuild: `docker-compose build && docker-compose up -d`

### Disable Deletion Propagation

Comment out the deletion detection sections in both sync methods, then rebuild.

### Change Time Window

Modify `timedelta(days=90)` in both `sync_google_to_icloud()` and `sync_icloud_to_google()` methods.

## Security Notes

- **OAuth tokens** are stored in `data/token.pickle` - keep this secure
- **iCloud password** is stored in plain text in `data/config.json` - use an app-specific password, not your main password
- **Notification URL** acts like a password - use a long random string (24+ characters)
- **Don't commit** `data/` directory to version control (.gitignore excludes it)

## License

MIT License - feel free to use and modify as needed.

## Support

For issues and questions:
- Check the troubleshooting section above
- Review `CLAUDE.md` for technical details
- Check Docker logs: `docker-compose logs -f`
