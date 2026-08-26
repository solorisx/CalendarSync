# CalendarSync

Bidirectional calendar synchronization between Google Calendar and iCloud Calendar. Runs as a Docker container with automatic sync intervals and optional notifications.

## Features

- ✅ **Bidirectional Sync** - Events sync both ways between Google and iCloud
- ➡️ **One-Way Mirror Calendars** - Mirror extra Google calendars into iCloud (with a title prefix), never synced back to Google
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
cd CalendarSync

# Run setup script
./scripts/setup.sh
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
  "oneway_google_calendars": [
    {
      "calendar_id": "team-calendar-id@group.calendar.google.com",
      "prefix": "[Team] "
    }
  ],
  "notify_url": "https://ntfy.sh/your-unique-channel"
}
```

**Configuration notes:**
- `google_calendar_id`: Use `"primary"` for your main calendar, or specific calendar ID
- `calendar_name`: Name of your iCloud calendar (usually "Calendar", run sync to see available names in error message if wrong)
- `oneway_google_calendars`: Optional - see [One-Way Mirror Calendars](#one-way-mirror-calendars) below. Omit or set to `[]` to disable.
- `notify_url`: Optional - set to `null` or `""` to disable notifications

#### One-Way Mirror Calendars

In addition to the bidirectional pair above, you can **one-way mirror** any number of
*extra* Google calendars into your iCloud calendar. Each mirrored event gets a
configurable title **prefix** so you can tell it apart from your regular events:

```json
"oneway_google_calendars": [
  { "calendar_id": "team-calendar-id@group.calendar.google.com", "prefix": "[Team] " },
  { "calendar_id": "another-id@group.calendar.google.com",       "prefix": "[Ops] " }
]
```

- `calendar_id`: The ID of the extra Google calendar to mirror (find it under Google
  Calendar → Settings → *Integrate calendar* → *Calendar ID*). The account authorized
  during setup must have read access to it.
- `prefix`: Text prepended to each event's title in iCloud (e.g. `"[Team] "`). Include a
  trailing space if you want one. The source event in Google is never modified, and the
  prefix never accumulates across syncs.

**Guarantees:**
- **One-way only.** These events flow Google → iCloud exclusively. They are **never**
  written back to Google (not to the source calendar, your primary, or any other), and
  they never interfere with the primary bidirectional sync.
- **Same iCloud calendar.** Mirrored events land in the calendar named by `calendar_name`,
  distinguished only by their prefix.
- **Deletions propagate.** Removing an event from the source Google calendar removes its
  mirrored copy from iCloud on the next sync.

> Note: because mirrored events exist only in iCloud (by design), the standalone
> `reconcile.py` audit reports them under "iCloud only" — this is expected and harmless.

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
# Direct Docker command
docker-compose logs -f

# Or use the helper script
./scripts/logs.sh          # Local (default)
./scripts/logs.sh remote   # Remote deployment
```

### Manual Sync

Trigger an immediate sync without waiting for the interval:

```bash
# Direct Docker command
docker-compose exec calendar-sync python sync_once.py

# Or use the helper script
./scripts/sync-now.sh          # Local (default)
./scripts/sync-now.sh remote   # Remote deployment
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
   - Fetches events from Google Calendar (yesterday to +5 years by default; configurable via `SYNC_PAST_DAYS` / `SYNC_FUTURE_DAYS`)
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

### Recurring Events

Recurring events sync as **real recurring series** (they carry their `RRULE`), so a
weekly meeting is a single recurring event on both sides — not dozens of copies.
Deleting a single occurrence propagates as an exclusion (`EXDATE`). Editing or moving a
*single* occurrence of a series (an override) is not synced separately in this version.

**One-time cutover after upgrading:** earlier versions expanded recurring series into many
per-occurrence copies. After upgrading, each series becomes one recurring event, but the old
copies remain in Google Calendar. They are **not** deleted automatically. To clean them up:

```bash
docker-compose run --rm calendar-sync python reconcile.py --list-fanout
```

This is **read-only** — it lists the leftover duplicate events (grouped by series) so you can
delete them by hand in Google Calendar. The sync state self-heals afterward.

### Known Limitation

Editing an **iCloud-originated** event on the **Google** side is not propagated back to iCloud
(iCloud is treated as the source of truth for those events). Edit such events in iCloud.

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
1. **Time range** - Syncs events from yesterday to +5 years by default (`SYNC_PAST_DAYS` / `SYNC_FUTURE_DAYS`)
2. **Already synced** - Check `data/sync_state.json` to see tracked events
3. **Wrong calendar** - Verify `google_calendar_id` and `calendar_name`
4. **API errors** - Rate limits, by default failed syncs will not be retried, reset state as below

**To reset sync state:**
```bash
echo '{"last_sync": null, "synced_events": {}, "last_error": null}' > data/sync_state.json
docker-compose restart
```

## Security Notes

- **OAuth tokens** are stored in `data/token.pickle` - keep this secure
- **iCloud password** is stored in plain text in `data/config.json` - use an app-specific password, not your main password
- **Notification URL** acts like a password - use a long random string (24+ characters)
- **Don't commit** `data/` directory to version control (.gitignore excludes it)

## License

MIT License - feel free to use and modify as needed.

## Deployment to Raspberry Pi

Deploy and manage CalendarSync on a remote Raspberry Pi:

### Initial Deployment

```bash
# Default (assumes raspberrypi hostname, pi user, ~/CalendarSync path)
./scripts/deploy.sh

# Custom settings
REMOTE_HOST=192.168.1.100 REMOTE_USER=myuser ./scripts/deploy.sh
```

The deploy script will:
- Test SSH connection
- Sync all project files (excluding data directory)
- Stop, rebuild, and restart containers
- Show recent logs

**First-time deployment:** The script will detect if this is the first deployment and guide you through setting up credentials.

### Remote Management

```bash
# View logs from remote
./scripts/logs.sh remote

# Trigger immediate sync on remote
./scripts/sync-now.sh remote

# SSH into the Pi
ssh pi@raspberrypi
cd ~/CalendarSync
```

### Environment Variables

Set these to customize remote connection (or use defaults):

```bash
export REMOTE_HOST=raspberrypi      # Default: raspberrypi
export REMOTE_USER=pi                # Default: pi
export REMOTE_PATH=~/CalendarSync    # Default: ~/CalendarSync
```

## Project Structure

```
CalendarSync/
├── scripts/              # Utility scripts
│   ├── setup.sh         # Initial setup
│   ├── deploy.sh        # Deploy to Raspberry Pi
│   ├── logs.sh          # View logs (local/remote)
│   └── sync-now.sh      # Trigger sync (local/remote)
├── data/                # Persistent data (gitignored)
│   ├── config.json      # Configuration
│   ├── credentials.json # Google OAuth credentials
│   ├── token.pickle     # Google OAuth tokens
│   └── sync_state.json  # Sync state tracking
├── sync_calendars.py    # Main sync service
├── sync_once.py         # One-time sync script
├── initial_auth.py      # OAuth setup helper
├── docker-compose.yml   # Docker configuration
├── Dockerfile           # Container definition
└── requirements.txt     # Python dependencies
```

## Support

For issues and questions:
- Check the troubleshooting section above
- Check Docker logs: `./scripts/logs.sh` or `docker-compose logs -f`
