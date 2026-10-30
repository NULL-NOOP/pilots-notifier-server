"""Application configuration and Firebase Admin initialization."""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import firebase_admin
from firebase_admin import credentials, messaging

LOCAL_TZ = ZoneInfo("America/New_York")
SERVICE_ACCOUNT_PATH = os.environ.get("FIREBASE_CREDENTIALS", "serviceAccountKey.json")
SESSIONS_FILE = os.environ.get("SESSIONS_FILE", "sessions.json")
DB_PATH = os.environ.get("DB_PATH", "pocketpilots.db")
# Older six-character codes are brute-forceable and must be rotated. The opt-in
# exists only for controlled test/transition environments.
ALLOW_LEGACY_PAIR_CODES = os.environ.get("ALLOW_LEGACY_PAIR_CODES", "false").lower() == "true"
FIREBASE_INITIALIZED = False


def initialize_firebase() -> bool:
    """Initialize Firebase when credentials are available; otherwise use simulation mode."""
    global FIREBASE_INITIALIZED

    if os.path.exists(SERVICE_ACCOUNT_PATH):
        try:
            if not firebase_admin._apps:
                credential = credentials.Certificate(SERVICE_ACCOUNT_PATH)
                firebase_admin.initialize_app(credential)
            FIREBASE_INITIALIZED = True
            print(
                f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] "
                f"Firebase Admin initialized with {SERVICE_ACCOUNT_PATH}"
            )
        except Exception as exc:
            print(
                f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] "
                f"Firebase Admin initialization error: {exc}"
            )
    else:
        print(
            f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] [WARNING] "
            f"'{SERVICE_ACCOUNT_PATH}' not found. Push notifications will run in SIMULATION mode."
        )
    return FIREBASE_INITIALIZED


initialize_firebase()
