# Pocket Pilots Notification API

FastAPI service that accepts flight telemetry, tracks paired devices, and sends Firebase Cloud Messaging notifications to the Android app.

## Related Repositories

- [Chrome extension](https://github.com/null-noop/pilots-notifier-extension)
- [Android app](https://github.com/null-noop/pilots-notifier-android)

## Local Setup

Requires Python 3, Java, Android SDK/ADB, and a running emulator or connected device for the combined workflow.

Copy `.env.example` to `.env`. Place the Firebase Admin SDK service-account JSON at the path set by `FIREBASE_CREDENTIALS` (default: `serviceAccountKey.json`). Keep credentials local; do not commit them.

To start the API and install/launch the Android debug app:

```bash
./run-local.sh
```

The runner creates `.venv` and installs `requirements.txt` on first run. It keeps the API on `http://127.0.0.1:8000` and stops it when you press Ctrl+C. The Android app and extension must use the same pairing code. Set the extension's API URL to `http://127.0.0.1:8000` for local testing.

The Android repository must also have `app/google-services.json`. It can be copied into place directly or decoded by the runner from `GOOGLE_SERVICES_BASE64` in the Android `.env` file.

## Tests

```bash
python -m pytest -q
```
