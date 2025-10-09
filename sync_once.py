#!/usr/bin/env python3
"""
Run a single sync operation (not a continuous loop).
Exits with code 0 on success, 1 on failure.
"""

import sys

# Add the app directory to path so we can import sync_calendars
sys.path.insert(0, '/app')

from sync_calendars import CalendarSync

if __name__ == "__main__":
    sync = CalendarSync()
    success = sync.run_sync()
    sys.exit(0 if success else 1)
