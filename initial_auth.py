#!/usr/bin/env python3
"""
One-time script to perform initial Google OAuth authentication.
Run this LOCALLY (not in Docker) to generate token.pickle.
"""

import os
import pickle
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

SCOPES = ['https://www.googleapis.com/auth/calendar']
TOKEN_FILE = './data/token.pickle'
CREDENTIALS_FILE = './data/credentials.json'

def authenticate():
    """Perform OAuth authentication"""
    creds = None

    if os.path.exists(TOKEN_FILE):
        print(f"Token already exists at {TOKEN_FILE}")
        with open(TOKEN_FILE, 'rb') as token:
            creds = pickle.load(token)

        if creds and creds.valid:
            print("✓ Existing token is valid!")
            return

        if creds and creds.expired and creds.refresh_token:
            print("Refreshing expired token...")
            creds.refresh(Request())
            with open(TOKEN_FILE, 'wb') as token:
                pickle.dump(creds, token)
            print("✓ Token refreshed!")
            return

    if not os.path.exists(CREDENTIALS_FILE):
        print(f"Error: {CREDENTIALS_FILE} not found")
        print("\nPlease download credentials.json from Google Cloud Console:")
        print("1. Go to: https://console.cloud.google.com/")
        print("2. Enable Google Calendar API")
        print("3. Create OAuth 2.0 credentials (Desktop app)")
        print("4. Download as 'credentials.json' to ./data/")
        return

    print("Starting OAuth flow...")
    print("A browser window will open for authentication.")

    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
    creds = flow.run_local_server(port=0)

    # Save the credentials
    with open(TOKEN_FILE, 'wb') as token:
        pickle.dump(creds, token)

    print(f"\n✓ Authentication successful!")
    print(f"✓ Token saved to {TOKEN_FILE}")
    print("\nYou can now run: docker-compose up -d")

if __name__ == "__main__":
    authenticate()
