#!/usr/bin/env bash
set -euo pipefail

SERVER_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ANDROID_DIR="$(cd -- "$SERVER_DIR/../pilots-notifier-android" && pwd)"
VENV_DIR="$SERVER_DIR/.venv"
ENV_FILE="$SERVER_DIR/.env"
ANDROID_ENV_FILE="$ANDROID_DIR/.env"
API_PID=""

if [[ -f "$ENV_FILE" ]]; then
  set -a
  source "$ENV_FILE"
  set +a
fi

if [[ -f "$ANDROID_ENV_FILE" ]]; then
  set -a
  source "$ANDROID_ENV_FILE"
  set +a
fi

FIREBASE_CREDENTIALS="${FIREBASE_CREDENTIALS:-serviceAccountKey.json}"
if [[ "$FIREBASE_CREDENTIALS" != /* ]]; then
  FIREBASE_CREDENTIALS="$SERVER_DIR/$FIREBASE_CREDENTIALS"
fi

for tool in adb java python3; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    printf 'Required command not found: %s\n' "$tool" >&2
    exit 1
  fi
done

if ! adb get-state >/dev/null 2>&1; then
  printf 'Start an Android emulator or connect a device, then run this again.\n' >&2
  exit 1
fi

if [[ -n "${GOOGLE_SERVICES_BASE64:-}" ]]; then
  (umask 077; printf '%s' "$GOOGLE_SERVICES_BASE64" | base64 --decode > "$ANDROID_DIR/app/google-services.json")
fi

for required_file in "$ANDROID_DIR/app/google-services.json" "$FIREBASE_CREDENTIALS"; do
  if [[ ! -f "$required_file" ]]; then
    printf 'Missing required Firebase configuration: %s\n' "$required_file" >&2
    exit 1
  fi
done

cd "$SERVER_DIR"

if [[ ! -x "$VENV_DIR/bin/uvicorn" ]]; then
  python3 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/python" -m pip install -r "$SERVER_DIR/requirements.txt"
fi

cleanup() {
  if [[ -n "$API_PID" ]]; then
    kill "$API_PID" 2>/dev/null || true
    wait "$API_PID" 2>/dev/null || true
  fi
  adb reverse --remove tcp:8000 >/dev/null 2>&1 || true
}
trap cleanup EXIT

"$VENV_DIR/bin/uvicorn" app:app --app-dir "$SERVER_DIR" --host 127.0.0.1 --port 8000 &
API_PID=$!

"$VENV_DIR/bin/python" -c 'import time, urllib.request
for _ in range(50):
    try:
        urllib.request.urlopen("http://127.0.0.1:8000/status", timeout=1).close()
        break
    except Exception:
        time.sleep(0.1)
else:
    raise SystemExit("Local API did not start on port 8000")'

"$ANDROID_DIR/gradlew" -p "$ANDROID_DIR" installDebug
adb reverse tcp:8000 tcp:8000
adb shell am force-stop com.nullnoop.pocket_pilots_notif
adb shell am start -n com.nullnoop.pocket_pilots_notif/.MainActivity --ez use_local_api true
printf 'Local API and Android app are running. Press Ctrl-C to stop the API.\n'
wait "$API_PID"
