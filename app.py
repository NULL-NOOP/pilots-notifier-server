import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import os
import random
import sqlite3
from zoneinfo import ZoneInfo

import firebase_admin
from fastapi import FastAPI, HTTPException, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from firebase_admin import credentials, messaging
from pydantic import BaseModel, Field

LOCAL_TZ = ZoneInfo("America/New_York")
SERVICE_ACCOUNT_PATH = os.environ.get("FIREBASE_CREDENTIALS", "serviceAccountKey.json")
SESSIONS_FILE = os.environ.get("SESSIONS_FILE", "sessions.json")
DB_PATH = os.environ.get("DB_PATH", "pocketpilots.db")

FIREBASE_INITIALIZED = False

# Rate limiting for failed authentication/lookup attempts
FAILED_ATTEMPTS: dict[str, list[float]] = {}


def check_rate_limit(request: Request):
    """Enforces rate limiting on failed/lookup attempts to prevent code brute-forcing."""
    client_ip = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        client_ip = forwarded.split(",")[0].strip()

    now = datetime.now().timestamp()
    attempts = [t for t in FAILED_ATTEMPTS.get(client_ip, []) if now - t < 60]
    FAILED_ATTEMPTS[client_ip] = attempts
    if len(attempts) >= 10:
        raise HTTPException(
            status_code=429,
            detail="Too many invalid attempts. Please wait 60 seconds before trying again."
        )


def record_failed_attempt(request: Request):
    """Records a failed lookup attempt against the caller's IP."""
    client_ip = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        client_ip = forwarded.split(",")[0].strip()
    now = datetime.now().timestamp()
    FAILED_ATTEMPTS.setdefault(client_ip, []).append(now)

if os.path.exists(SERVICE_ACCOUNT_PATH):
    try:
        if not firebase_admin._apps:
            cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
            firebase_admin.initialize_app(cred)
        FIREBASE_INITIALIZED = True
        print(f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] Firebase Admin initialized with {SERVICE_ACCOUNT_PATH}")
    except Exception as e:
        print(f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] Firebase Admin initialization error: {e}")
else:
    print(f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] [WARNING] '{SERVICE_ACCOUNT_PATH}' not found. Push notifications will run in SIMULATION mode.")

app = FastAPI(
    title="Pocket Pilots Notification Server",
    description="Multi-device, multi-user pairing and FCM notification backend for Pocket Pilots",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class FlightSession:
    """Manages active devices and flight status for a single pairing code."""
    def __init__(self, pair_code: str):
        self.pair_code: str = pair_code.strip().replace(" ", "").upper()
        # Mapping: device_id -> fcm_token
        self.devices: dict[str, str] = {}
        self.current_flight: dict = {
            "id": None,
            "last_status": None,
            "route": None,
            "phase": None,
            "plane_type": None,
            "progress": 0,
            "updated_at": None,
        }
        self.flight_task: asyncio.Task | None = None
        self.created_at: datetime = datetime.now(LOCAL_TZ)
        self.last_seen: datetime = datetime.now(LOCAL_TZ)

    def add_token(self, device_id: str, token: str):
        self.last_seen = datetime.now(LOCAL_TZ)
        device_id = device_id.strip() if device_id else "android_main"

        # If token already exists under another device_id, prune old entry
        for eid, etok in list(self.devices.items()):
            if etok == token and eid != device_id:
                del self.devices[eid]

        # Prevent overwriting another device if both use default "android_main"
        if device_id == "android_main" and device_id in self.devices and self.devices[device_id] != token:
            idx = 2
            while f"android_main_{idx}" in self.devices:
                idx += 1
            device_id = f"android_main_{idx}"

        self.devices[device_id] = token

    def remove_device(self, device_id: str):
        self.last_seen = datetime.now(LOCAL_TZ)
        self.devices.pop(device_id, None)

    def remove_token(self, token: str):
        self.last_seen = datetime.now(LOCAL_TZ)
        to_del = [d for d, t in self.devices.items() if t == token]
        for d in to_del:
            del self.devices[d]

    @property
    def fcm_tokens(self) -> list[str]:
        return list(dict.fromkeys(self.devices.values()))

    def cancel_task(self):
        if self.flight_task and not self.flight_task.done():
            self.flight_task.cancel()
        self.flight_task = None


SESSIONS: dict[str, FlightSession] = {}


@contextmanager
def get_db():
    """Provides a managed SQLite database connection that automatically commits and closes."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    """Initializes SQLite tables for persistent pairing and flight data."""
    try:
        with get_db() as conn:
            with conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA foreign_keys=ON;")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS sessions (
                        pair_code TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL,
                        last_seen TEXT NOT NULL
                    );
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS devices (
                        pair_code TEXT NOT NULL,
                        device_id TEXT NOT NULL,
                        token TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (pair_code, device_id),
                        FOREIGN KEY (pair_code) REFERENCES sessions(pair_code) ON DELETE CASCADE
                    );
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS active_flights (
                        pair_code TEXT PRIMARY KEY,
                        flight_id TEXT,
                        route TEXT,
                        plane_type TEXT,
                        t0 REAL,
                        total_sec REAL,
                        arrival_sec REAL,
                        bounds_json TEXT,
                        segments_json TEXT,
                        last_status INTEGER,
                        phase TEXT,
                        progress INTEGER,
                        updated_at TEXT,
                        FOREIGN KEY (pair_code) REFERENCES sessions(pair_code) ON DELETE CASCADE
                    );
                """)
                # Migrations for existing databases
                for col, col_type in [
                    ("t0", "REAL"),
                    ("total_sec", "REAL"),
                    ("arrival_sec", "REAL"),
                    ("bounds_json", "TEXT"),
                    ("segments_json", "TEXT")
                ]:
                    try:
                        conn.execute(f"ALTER TABLE active_flights ADD COLUMN {col} {col_type};")
                    except Exception:
                        pass
    except Exception as e:
        print(f"Error initializing database {DB_PATH}: {e}")


init_db()


def db_save_session(session: FlightSession):
    """Saves a single session and its registered devices to SQLite."""
    try:
        with get_db() as conn:
            with conn:
                conn.execute("PRAGMA foreign_keys=ON;")
                # Upsert session
                conn.execute("""
                    INSERT INTO sessions (pair_code, created_at, last_seen)
                    VALUES (?, ?, ?)
                    ON CONFLICT(pair_code) DO UPDATE SET last_seen=excluded.last_seen;
                """, (session.pair_code, session.created_at.isoformat(), session.last_seen.isoformat()))

                # Synchronize devices
                device_ids = list(session.devices.keys())
                if device_ids:
                    placeholders = ",".join("?" * len(device_ids))
                    conn.execute(
                        f"DELETE FROM devices WHERE pair_code=? AND device_id NOT IN ({placeholders})",
                        [session.pair_code] + device_ids
                    )
                else:
                    conn.execute("DELETE FROM devices WHERE pair_code=?", (session.pair_code,))

                now_iso = datetime.now(LOCAL_TZ).isoformat()
                for dev_id, tok in session.devices.items():
                    conn.execute("""
                        INSERT INTO devices (pair_code, device_id, token, updated_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(pair_code, device_id) DO UPDATE SET token=excluded.token, updated_at=excluded.updated_at;
                    """, (session.pair_code, dev_id, tok, now_iso))

                # Upsert active flight
                fl = session.current_flight
                if fl.get("id"):
                    conn.execute("""
                        INSERT INTO active_flights (
                            pair_code, flight_id, route, plane_type,
                            t0, total_sec, arrival_sec, bounds_json, segments_json,
                            last_status, phase, progress, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(pair_code) DO UPDATE SET
                            flight_id=excluded.flight_id,
                            route=excluded.route,
                            plane_type=excluded.plane_type,
                            t0=excluded.t0,
                            total_sec=excluded.total_sec,
                            arrival_sec=excluded.arrival_sec,
                            bounds_json=excluded.bounds_json,
                            segments_json=excluded.segments_json,
                            last_status=excluded.last_status,
                            phase=excluded.phase,
                            progress=excluded.progress,
                            updated_at=excluded.updated_at;
                    """, (
                        session.pair_code,
                        fl.get("id"),
                        fl.get("route"),
                        fl.get("plane_type"),
                        fl.get("t0"),
                        fl.get("total_sec"),
                        fl.get("arrival_sec"),
                        json.dumps(fl.get("bounds")) if fl.get("bounds") else None,
                        json.dumps(fl.get("segments")) if fl.get("segments") else None,
                        fl.get("last_status"),
                        fl.get("phase"),
                        fl.get("progress", 0),
                        fl.get("updated_at") or now_iso
                    ))
    except Exception as e:
        print(f"Error saving session {session.pair_code} to DB: {e}")


def save_sessions(session: FlightSession | None = None):
    """Persists sessions to SQLite and mirrors to sessions.json backup."""
    if session:
        db_save_session(session)
    else:
        for s in list(SESSIONS.values()):
            db_save_session(s)

    # Mirror to JSON file as an accessible text snapshot
    try:
        data = {}
        for code, s in SESSIONS.items():
            if s.devices:
                data[code] = {
                    "pair_code": s.pair_code,
                    "devices": s.devices,
                    "created_at": s.created_at.isoformat(),
                    "last_seen": s.last_seen.isoformat(),
                    "current_flight": s.current_flight if s.current_flight.get("id") else None
                }
        temp_file = f"{SESSIONS_FILE}.tmp"
        with open(temp_file, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(temp_file, SESSIONS_FILE)
    except Exception as e:
        print(f"Error mirroring sessions to {SESSIONS_FILE}: {e}")


def load_sessions():
    """Loads saved sessions and active flights from SQLite (with fallback migration from JSON)."""
    loaded_from_db = False
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT * FROM sessions").fetchall()
            for r in rows:
                code = r["pair_code"].strip().replace(" ", "").upper()
                s = FlightSession(code)
                try:
                    s.created_at = datetime.fromisoformat(r["created_at"])
                    s.last_seen = datetime.fromisoformat(r["last_seen"])
                except Exception:
                    pass

                dev_rows = conn.execute("SELECT * FROM devices WHERE pair_code=?", (code,)).fetchall()
                for d in dev_rows:
                    s.devices[d["device_id"]] = d["token"]

                fl_row = conn.execute("SELECT * FROM active_flights WHERE pair_code=?", (code,)).fetchone()
                if fl_row and fl_row["flight_id"]:
                    bounds = None
                    segments = None
                    try:
                        if fl_row["bounds_json"]:
                            bounds = json.loads(fl_row["bounds_json"])
                    except Exception: pass
                    try:
                        if fl_row["segments_json"]:
                            segments = json.loads(fl_row["segments_json"])
                    except Exception: pass

                    s.current_flight = {
                        "id": fl_row["flight_id"],
                        "route": fl_row["route"],
                        "plane_type": fl_row["plane_type"],
                        "t0": fl_row["t0"] if "t0" in fl_row.keys() else None,
                        "total_sec": fl_row["total_sec"] if "total_sec" in fl_row.keys() else None,
                        "arrival_sec": fl_row["arrival_sec"] if "arrival_sec" in fl_row.keys() else None,
                        "bounds": bounds,
                        "segments": segments,
                        "last_status": fl_row["last_status"],
                        "phase": fl_row["phase"],
                        "progress": fl_row["progress"],
                        "updated_at": fl_row["updated_at"],
                    }

                SESSIONS[code] = s
                loaded_from_db = True

        if loaded_from_db and SESSIONS:
            print(f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] Loaded {len(SESSIONS)} session(s) from persistent database ({DB_PATH})")
            return
    except Exception as e:
        print(f"Database load error ({DB_PATH}): {e}")

    # Fallback / Initial migration: load from sessions.json if DB was empty
    if os.path.exists(SESSIONS_FILE):
        try:
            with open(SESSIONS_FILE, "r") as f:
                data = json.load(f)
            for code, item in data.items():
                norm_code = code.strip().replace(" ", "").upper()
                s = FlightSession(norm_code)
                s.devices = item.get("devices", {})
                try:
                    s.created_at = datetime.fromisoformat(item["created_at"])
                    s.last_seen = datetime.fromisoformat(item["last_seen"])
                except Exception:
                    pass
                if "current_flight" in item and item["current_flight"]:
                    s.current_flight = item["current_flight"]
                SESSIONS[norm_code] = s
                db_save_session(s)
            print(f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] Migrated {len(SESSIONS)} session(s) from {SESSIONS_FILE} into database")
        except Exception as e:
            print(f"Error loading fallback sessions from {SESSIONS_FILE}: {e}")


load_sessions()


def generate_unique_pair_code() -> str:
    """
    Generates a secure, unguessable 6-character alphanumeric pairing code (>1 billion combinations).
    Formatted as XXX-XXX for clean human readability while preventing brute-force enumeration.
    """
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    for _ in range(1000):
        p1 = "".join(random.choices(alphabet, k=3))
        p2 = "".join(random.choices(alphabet, k=3))
        code = f"{p1}-{p2}"
        norm = code.replace("-", "")
        if norm not in SESSIONS:
            return code
    p1 = "".join(random.choices(alphabet, k=4))
    p2 = "".join(random.choices(alphabet, k=4))
    return f"{p1}-{p2}"


def get_or_create_session(pair_code: str) -> FlightSession:
    raw = pair_code.strip().upper()
    norm = raw.replace(" ", "").replace("-", "")
    if norm not in SESSIONS:
        s = FlightSession(norm)
        SESSIONS[norm] = s
    if raw != norm and raw not in SESSIONS:
        SESSIONS[raw] = SESSIONS[norm]
    return SESSIONS[norm]


def resolve_session(
    path_code: str | None = None,
    query_code: str | None = None,
    query_pair_code: str | None = None,
    header_code: str | None = None,
    body_pair_code: str | None = None,
    body_code: str | None = None,
) -> FlightSession | None:
    """
    Strictly resolves session from path, query, header, or body.
    Never falls back to another user's session or a default pool.
    """
    candidate = (
        path_code
        or query_code
        or query_pair_code
        or header_code
        or body_pair_code
        or body_code
    )
    if not candidate or not candidate.strip():
        return None

    norm = candidate.strip().replace(" ", "").replace("-", "").upper()
    return SESSIONS.get(norm)


class TokenRegistration(BaseModel):
    token: str
    device_id: str = "android_main"
    pair_code: str | None = None
    code: str | None = None


class TokenUnregistration(BaseModel):
    token: str | None = None
    device_id: str | None = None
    pair_code: str | None = None
    code: str | None = None


class FlightPayload(BaseModel):
    id: str
    t0: float
    now: float
    origin: str
    dest: str
    planeType: str
    trk: list[float]
    pair_code: str | None = None
    code: str | None = None


def analyze_flight_profile(trk: list[float], total_sec: float) -> tuple[dict, list[int]]:
    """
    Parses altitude profile to find climb completion and descent initiation,
    generating a 5-segment percentage array: [5, asc%, cruise%, desc%, 5].
    """
    points = []
    for i in range(0, len(trk), 4):
        if i + 3 < len(trk):
            points.append((trk[i], trk[i + 3]))  # (t, alt)

    if not points or total_sec <= 60.0:
        return {
            "t_takeoff": 30.0,
            "t_cruise_start": 30.0 + (total_sec - 60.0) * 0.25,
            "t_cruise_end": 30.0 + (total_sec - 60.0) * 0.75,
            "t_landed": max(30.0, total_sec - 30.0)
        }, [5, 22, 46, 22, 5]

    min_alt = min(p[1] for p in points)
    max_alt = max(p[1] for p in points)
    alt_range = max_alt - min_alt

    cruise_thresh = min_alt + (0.95 * alt_range) if alt_range > 50 else min_alt

    t_cruise_start = next((p[0] for p in points if p[1] >= cruise_thresh), 30.0)
    t_cruise_end = next((p[0] for p in reversed(points) if p[1] >= cruise_thresh), total_sec - 30.0)

    t_cruise_start = max(30.0, min(t_cruise_start, total_sec - 30.0))
    t_cruise_end = max(t_cruise_start, min(t_cruise_end, total_sec - 30.0))

    asc_dur = t_cruise_start - 30.0
    cruise_dur = t_cruise_end - t_cruise_start
    desc_dur = (total_sec - 30.0) - t_cruise_end
    mid_dur = max(1.0, total_sec - 60.0)

    pct_asc = max(5.0, 90.0 * (asc_dur / mid_dur))
    pct_cruise = max(10.0, 90.0 * (cruise_dur / mid_dur))
    pct_desc = max(5.0, 90.0 * (desc_dur / mid_dur))

    mid_sum = pct_asc + pct_cruise + pct_desc
    scale = 90.0 / mid_sum

    s_asc = max(5, int(round(pct_asc * scale)))
    s_cruise = max(10, int(round(pct_cruise * scale)))
    s_desc = max(5, 90 - (s_asc + s_cruise))

    segments = [5, s_asc, s_cruise, s_desc, 5]

    bounds = {
        "t_takeoff": 30.0,
        "t_cruise_start": t_cruise_start,
        "t_cruise_end": t_cruise_end,
        "t_landed": max(30.0, total_sec - 30.0)
    }

    return bounds, segments


def get_stage_from_bounds(elapsed_sec: float, total_sec: float, bounds: dict) -> tuple[int, str]:
    """Maps elapsed time to stages 1 through 5."""
    if elapsed_sec <= bounds["t_takeoff"]:
        return 1, "Takeoff"
    elif elapsed_sec <= bounds["t_cruise_start"]:
        return 2, "Ascent"
    elif elapsed_sec <= bounds["t_cruise_end"]:
        return 3, "Cruise"
    elif elapsed_sec < bounds["t_landed"]:
        return 4, "Descent"
    else:
        return 5, "Landed"


async def send_fcm_notification(
    title: str, 
    message: str, 
    status_code: int, 
    phase_label: str,
    segments: list[int],
    route: str = "", 
    progress: int = 0, 
    arrival_sec: float = None,
    tokens: list[str] | None = None,
    session: FlightSession | None = None,
) -> dict:
    """Dispatches FCM push notifications to all specified tokens or session tokens."""
    if tokens is None:
        if session:
            tokens = session.fcm_tokens
        else:
            print(f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] Push rejected: No session or target tokens provided.")
            return {"sent": 0, "failed": 0, "total": 0}

    code_label = session.pair_code if session else "GLOBAL"

    if not tokens:
        print(f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] [{code_label}] No FCM tokens registered. Skipping push.")
        return {"sent": 0, "failed": 0, "total": 0}

    data_payload = {
        "title": title,
        "message": message,
        "route": route,
        "progress": str(progress),
        "arrival_sec": str(arrival_sec) if arrival_sec else "0",
        "status": str(status_code),
        "phase": phase_label,
        "segments": ",".join(map(str, segments))
    }

    if not FIREBASE_INITIALIZED:
        print(f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] [{code_label}] [SIMULATION] Push to {len(tokens)} device(s) ({phase_label} | Code {status_code}): {title} - {message}")
        return {"sent": len(tokens), "failed": 0, "total": len(tokens)}

    def _send_one(t: str):
        msg = messaging.Message(
            token=t,
            android=messaging.AndroidConfig(priority="high", ttl=0),
            data=data_payload
        )
        return messaging.send(msg)

    sent = 0
    failed = 0
    stale_tokens = []

    for t in tokens:
        try:
            resp = await asyncio.to_thread(_send_one, t)
            sent += 1
            print(f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] [{code_label}] FCM Push Sent to {t[:12]}... ({phase_label} | Code {status_code}): {resp}")
        except messaging.UnregisteredError:
            failed += 1
            stale_tokens.append(t)
            print(f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] [{code_label}] FCM Token Unregistered (pruning): {t[:12]}...")
        except Exception as e:
            failed += 1
            print(f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] [{code_label}] FCM Push Error for {t[:12]}...: {e}")

    if stale_tokens and session:
        for st in stale_tokens:
            session.remove_token(st)
        save_sessions()

    return {"sent": sent, "failed": failed, "total": len(tokens)}


async def handle_flight_timer(
    session: FlightSession,
    flight_id: str, 
    route: str, 
    total_sec: float, 
    elapsed_sec: float, 
    arrival_sec: float, 
    plane_type: str,
    bounds: dict,
    segments: list[int]
):
    """Background task that steps through phase transitions using wall-clock time."""
    try:
        import time
        start_wall_time = time.time()
        initial_elapsed = elapsed_sec

        while True:
            await asyncio.sleep(5.0)  # Check progress every 5 seconds
            
            # If a different flight has started in this session, terminate this timer
            if session.current_flight.get("id") != flight_id:
                break

            # Calculate real-world elapsed time (immune to server sleep/CPU throttling drift)
            wall_delta = time.time() - start_wall_time
            curr_elapsed = min(total_sec, initial_elapsed + wall_delta)

            status_code, phase_label = get_stage_from_bounds(curr_elapsed, total_sec, bounds)
            progress_pct = min(100.0, (curr_elapsed / total_sec * 100.0)) if total_sec > 0 else 100.0
            if status_code == 5:
                progress_pct = 100.0

            if session.current_flight.get("last_status") != status_code:
                session.current_flight["last_status"] = status_code
                session.current_flight["phase"] = phase_label
                session.current_flight["progress"] = int(progress_pct)
                session.current_flight["updated_at"] = datetime.now(LOCAL_TZ).isoformat()
                save_sessions(session)

                remaining_sec = max(0.0, total_sec - curr_elapsed)
                arrival_dt = datetime.now(LOCAL_TZ) + timedelta(seconds=remaining_sec)
                arrival_str = arrival_dt.strftime('%I:%M %p').lstrip('0')

                tokens = session.fcm_tokens
                if status_code == 5:
                    dest = route.split('➔')[1].strip() if '➔' in route else "destination"
                    await send_fcm_notification(
                        title=f"{route} - Landed!",
                        message=f"Touchdown at {dest} ({plane_type})",
                        status_code=5,
                        phase_label="Landed",
                        segments=segments,
                        route=route,
                        progress=100,
                        tokens=tokens,
                        session=session
                    )
                    break
                else:
                    await send_fcm_notification(
                        title=f"{route} ({phase_label})",
                        message=f"ETA {arrival_str} · {plane_type}",
                        status_code=status_code,
                        phase_label=phase_label,
                        segments=segments,
                        route=route,
                        progress=int(progress_pct),
                        arrival_sec=arrival_sec,
                        tokens=tokens,
                        session=session
                    )

            if curr_elapsed >= total_sec:
                break
    except asyncio.CancelledError:
        pass


def resume_active_flight_timers():
    """Resumes timers for any in-progress flights (e.g. 6hr+ flights) after server restart."""
    import time
    now_sec = time.time()
    for session in SESSIONS.values():
        fl = session.current_flight
        if fl and fl.get("id") and fl.get("last_status") != 5:
            t0 = fl.get("t0")
            total_sec = fl.get("total_sec")
            bounds = fl.get("bounds")
            segments = fl.get("segments")
            if t0 and total_sec and bounds and segments:
                elapsed_sec = max(0.0, now_sec - t0)
                if elapsed_sec < total_sec + 120:  # Still in flight or just landing
                    print(f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] [{session.pair_code}] Resuming in-progress flight timer: {fl.get('id')} ({fl.get('route')}). Elapsed: {int(elapsed_sec)}s / {int(total_sec)}s")
                    session.cancel_task()
                    session.flight_task = asyncio.create_task(
                        handle_flight_timer(
                            session=session,
                            flight_id=fl["id"],
                            route=fl["route"],
                            total_sec=total_sec,
                            elapsed_sec=elapsed_sec,
                            arrival_sec=fl.get("arrival_sec", t0 + total_sec),
                            plane_type=fl.get("plane_type", "Aircraft"),
                            bounds=bounds,
                            segments=segments
                        )
                    )


@app.on_event("startup")
async def startup_event():
    resume_active_flight_timers()


@app.post("/webhook/flight")
@app.post("/webhook/flight/{path_pair_code}")
async def receive_flight_update(
    payload: FlightPayload,
    request: Request,
    path_pair_code: str | None = None,
    code: str | None = Query(default=None),
    pair_code: str | None = Query(default=None),
    x_pair_code: str | None = Header(default=None, alias="X-Pair-Code"),
    x_code: str | None = Header(default=None, alias="X-Code"),
):
    check_rate_limit(request)
    session = resolve_session(
        path_code=path_pair_code,
        query_code=code,
        query_pair_code=pair_code,
        header_code=x_pair_code or x_code,
        body_pair_code=payload.pair_code,
        body_code=payload.code,
    )

    if not session:
        record_failed_attempt(request)
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid link code. Please provide a registered pair code in the URL, header, or body."
        )

    t0_sec = payload.t0 / 1000.0 if payload.t0 > 1e11 else payload.t0
    now_sec = payload.now / 1000.0 if payload.now > 1e11 else payload.now
    total_sec = payload.trk[-4] if len(payload.trk) >= 4 else 0.0
    
    elapsed_sec = max(0.0, now_sec - t0_sec)
    remaining_sec = max(0.0, total_sec - elapsed_sec)
    arrival_sec = now_sec + remaining_sec
    progress_pct = min(100.0, (elapsed_sec / total_sec * 100.0)) if total_sec > 0 else 100.0

    bounds, segments = analyze_flight_profile(payload.trk, total_sec)
    status_code, phase_label = get_stage_from_bounds(elapsed_sec, total_sec, bounds)
    if status_code == 5:
        progress_pct = 100.0
    
    route_str = f"{payload.origin} ➔ {payload.dest}"

    if session.current_flight.get("id") != payload.id:
        session.current_flight["id"] = payload.id
        session.current_flight["last_status"] = None

    session.current_flight["last_status"] = status_code
    session.current_flight["route"] = route_str
    session.current_flight["phase"] = phase_label
    session.current_flight["plane_type"] = payload.planeType
    session.current_flight["progress"] = int(progress_pct)
    session.current_flight["t0"] = t0_sec
    session.current_flight["total_sec"] = total_sec
    session.current_flight["arrival_sec"] = arrival_sec
    session.current_flight["bounds"] = bounds
    session.current_flight["segments"] = segments
    session.current_flight["updated_at"] = datetime.now(LOCAL_TZ).isoformat()
    save_sessions(session)

    arrival_dt = datetime.now(LOCAL_TZ) + timedelta(seconds=remaining_sec)
    arrival_str = arrival_dt.strftime('%I:%M %p').lstrip('0')

    tokens = session.fcm_tokens

    # Send notification for incoming webhook payload
    if status_code == 5:
        await send_fcm_notification(
            title=f"{route_str} - Landed!",
            message=f"Touchdown at {payload.dest} ({payload.planeType})",
            status_code=5,
            phase_label="Landed",
            segments=segments,
            route=route_str,
            progress=100,
            tokens=tokens,
            session=session
        )
    else:
        await send_fcm_notification(
            title=f"{route_str} ({phase_label})",
            message=f"ETA {arrival_str} · {payload.planeType}",
            status_code=status_code,
            phase_label=phase_label,
            segments=segments,
            route=route_str,
            progress=int(progress_pct),
            arrival_sec=arrival_sec,
            tokens=tokens,
            session=session
        )

    # Cancel previous background timer for this session and start a new one
    session.cancel_task()
    session.flight_task = asyncio.create_task(
        handle_flight_timer(
            session,
            payload.id,
            route_str,
            total_sec,
            elapsed_sec,
            arrival_sec,
            payload.planeType,
            bounds,
            segments
        )
    )

    return {
        "status": "Telemetry processed",
        "pair_code": session.pair_code,
        "devices_notified": len(tokens),
        "phase": phase_label,
        "status_code": status_code,
        "segments": segments,
        "progress_pct": round(progress_pct, 1),
        "remaining_sec": round(remaining_sec, 1)
    }


@app.post("/register-token")
async def register_token(data: TokenRegistration):
    raw_code = data.pair_code or data.code
    is_new = False
    if raw_code and raw_code.strip():
        pair_code = raw_code.strip().replace(" ", "").upper()
    else:
        pair_code = generate_unique_pair_code()
        is_new = True

    session = get_or_create_session(pair_code)
    session.add_token(data.device_id, data.token)

    save_sessions(session)

    print(f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] Registered device '{data.device_id}' to pair code [{pair_code}]. Total devices: {len(session.devices)}")
    return {
        "status": "success",
        "pair_code": pair_code,
        "is_new_code": is_new,
        "device_id": data.device_id,
        "devices_count": len(session.devices),
        "devices": list(session.devices.keys()),
        "message": f"Device '{data.device_id}' linked to pair code {pair_code}"
    }


@app.post("/unregister-token")
async def unregister_token(data: TokenUnregistration):
    raw_code = data.pair_code or data.code
    target_sessions = []
    if raw_code and raw_code.strip():
        norm = raw_code.strip().replace(" ", "").upper()
        if norm in SESSIONS:
            target_sessions.append(SESSIONS[norm])
    else:
        target_sessions = list(SESSIONS.values())

    removed = 0
    for s in target_sessions:
        if data.device_id and data.device_id in s.devices:
            s.remove_device(data.device_id)
            removed += 1
        elif data.token:
            before = len(s.devices)
            s.remove_token(data.token)
            removed += (before - len(s.devices))

    save_sessions()
    return {"status": "success", "removed_count": removed}


@app.post("/pair/new")
@app.get("/pair/new")
async def create_new_pair():
    code = generate_unique_pair_code()
    session = get_or_create_session(code)
    save_sessions()
    return {
        "status": "success",
        "pair_code": code,
        "message": f"Pair code {code} generated. Enter this in your app or Chrome extension to link."
    }


@app.get("/pair/{pair_code}")
async def get_pair_status(pair_code: str, request: Request):
    check_rate_limit(request)
    norm_code = pair_code.strip().replace(" ", "").replace("-", "").upper()
    session = SESSIONS.get(norm_code)
    if not session:
        record_failed_attempt(request)
        raise HTTPException(status_code=404, detail="Pair code not found.")

    return {
        "exists": True,
        "pair_code": norm_code,
        "device_count": len(session.devices),
        "devices": list(session.devices.keys()),
        "fcm_token_registered": len(session.devices) > 0,
        "has_active_flight": bool(session.current_flight.get("id")),
        "active_flight": session.current_flight,
        "last_seen": session.last_seen.isoformat()
    }


@app.post("/pair/{pair_code}/test")
async def send_test_push(pair_code: str, request: Request):
    check_rate_limit(request)
    norm_code = pair_code.strip().replace(" ", "").replace("-", "").upper()
    session = SESSIONS.get(norm_code)
    if not session:
        record_failed_attempt(request)
        raise HTTPException(status_code=404, detail=f"Pair code '{norm_code}' not found.")
    if not session.fcm_tokens:
        raise HTTPException(status_code=400, detail=f"No devices / FCM tokens registered under code '{norm_code}'.")

    res = await send_fcm_notification(
        title="Pocket Pilots Connected! ✈️",
        message="Your device is successfully linked to Pocket Pilots.",
        status_code=1,
        phase_label="Linked",
        segments=[5, 20, 50, 20, 5],
        route="Extension ➔ App",
        progress=100,
        tokens=session.fcm_tokens,
        session=session
    )
    return {
        "status": "success",
        "pair_code": norm_code,
        "message": f"Dispatched test push to {res['sent']} device(s).",
        "results": res
    }


@app.get("/status")
async def get_status(
    request: Request,
    code: str | None = Query(default=None),
    pair_code: str | None = Query(default=None)
):
    target_code = pair_code or code
    if target_code:
        check_rate_limit(request)
        norm = target_code.strip().replace(" ", "").replace("-", "").upper()
        session = SESSIONS.get(norm)
        if not session:
            record_failed_attempt(request)
            raise HTTPException(status_code=404, detail=f"Pair code '{norm}' not found.")
        return {
            "status": "ok",
            "pair_code": norm,
            "fcm_token_registered": len(session.devices) > 0,
            "device_count": len(session.devices),
            "devices": list(session.devices.keys()),
            "active_flight": session.current_flight
        }

    # Public health check endpoint: Zero private data or user counts are leaked
    return {
        "status": "ok",
        "service": "Pocket Pilots Notification Hub",
        "firebase_live": FIREBASE_INITIALIZED,
        "version": "2.1.0"
    }


@app.get("/", response_class=HTMLResponse)
@app.get("/pair", response_class=HTMLResponse)
async def get_dashboard(request: Request):
    # If API client requests application/json, return public status JSON instead
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse(await get_status(request=request))

    fb_status_color = "#10b981" if FIREBASE_INITIALIZED else "#f59e0b"
    fb_status_text = "Initialized (Live FCM)" if FIREBASE_INITIALIZED else "Simulation Mode (No Key)"

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Pocket Pilots Notification Hub</title>
  <style>
    :root {{
      --bg: #0f172a;
      --card: #1e293b;
      --card-border: #334155;
      --accent: #38bdf8;
      --accent-hover: #0284c7;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --success: #10b981;
      --warning: #f59e0b;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; }}
    body {{ background: var(--bg); color: var(--text); padding: 2rem 1rem; line-height: 1.5; }}
    .container {{ max-width: 960px; margin: 0 auto; }}
    header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 2rem; border-bottom: 1px solid var(--card-border); padding-bottom: 1.25rem; flex-wrap: wrap; gap: 1rem; }}
    .logo-group {{ display: flex; align-items: center; gap: 0.75rem; }}
    .logo-icon {{ font-size: 2rem; }}
    h1 {{ font-size: 1.5rem; font-weight: 700; color: var(--text); }}
    .badge {{ display: inline-flex; align-items: center; gap: 0.4rem; padding: 0.35rem 0.75rem; border-radius: 9999px; font-size: 0.8rem; font-weight: 600; background: #0f172a; border: 1px solid var(--card-border); }}
    .badge-dot {{ width: 8px; height: 8px; border-radius: 50%; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 1rem; margin-bottom: 2rem; }}
    .stat-card {{ background: var(--card); border: 1px solid var(--card-border); border-radius: 0.75rem; padding: 1.25rem; }}
    .stat-label {{ font-size: 0.85rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.5rem; }}
    .stat-val {{ font-size: 1.35rem; font-weight: 700; color: var(--accent); }}
    .tabs {{ display: flex; gap: 0.5rem; margin-bottom: 1.5rem; border-bottom: 1px solid var(--card-border); padding-bottom: 0.5rem; }}
    .tab-btn {{ background: none; border: none; color: var(--text-muted); padding: 0.5rem 1rem; cursor: pointer; font-size: 0.95rem; font-weight: 600; border-radius: 0.5rem; }}
    .tab-btn.active {{ color: var(--accent); background: rgba(56, 189, 248, 0.1); }}
    .card {{ background: var(--card); border: 1px solid var(--card-border); border-radius: 0.75rem; padding: 1.5rem; margin-bottom: 1.5rem; }}
    .card h2 {{ font-size: 1.2rem; margin-bottom: 1rem; display: flex; align-items: center; gap: 0.5rem; }}
    .input-row {{ display: flex; gap: 0.75rem; margin-bottom: 1rem; flex-wrap: wrap; }}
    input, select {{ background: #0f172a; border: 1px solid var(--card-border); border-radius: 0.5rem; color: var(--text); padding: 0.65rem 1rem; font-size: 0.95rem; flex: 1; min-width: 180px; }}
    input:focus {{ outline: none; border-color: var(--accent); }}
    button {{ background: var(--accent); color: #0f172a; border: none; border-radius: 0.5rem; padding: 0.65rem 1.25rem; font-weight: 600; cursor: pointer; transition: background 0.15s; font-size: 0.95rem; }}
    button:hover {{ background: var(--accent-hover); }}
    button.secondary {{ background: #334155; color: var(--text); }}
    button.secondary:hover {{ background: #475569; }}
    .output-box {{ background: #0b1120; border: 1px solid var(--card-border); border-radius: 0.5rem; padding: 1rem; font-family: monospace; font-size: 0.85rem; max-height: 240px; overflow-y: auto; white-space: pre-wrap; color: #38bdf8; margin-top: 1rem; }}
    .code-block {{ background: #0b1120; border: 1px solid var(--card-border); border-radius: 0.5rem; padding: 1rem; font-family: monospace; font-size: 0.85rem; overflow-x: auto; white-space: pre; color: #e2e8f0; margin: 0.75rem 0; }}
    .step {{ margin-bottom: 1.25rem; }}
    .step-num {{ display: inline-block; width: 24px; height: 24px; background: var(--accent); color: #0f172a; border-radius: 50%; text-align: center; font-weight: 700; font-size: 0.85rem; line-height: 24px; margin-right: 0.5rem; }}
    .tab-content {{ display: none; }}
    .tab-content.active {{ display: block; }}
  </style>
</head>
<body>
<div class="container">
  <header>
    <div class="logo-group">
      <span class="logo-icon">✈️</span>
      <div>
        <h1>Pocket Pilots Notification Server</h1>
        <p style="font-size: 0.85rem; color: var(--text-muted);">Multi-User Private Companion Hub for App & Chrome Extension</p>
      </div>
    </div>
    <div class="badge">
      <span class="badge-dot" style="background: {fb_status_color};"></span>
      <span>{fb_status_text}</span>
    </div>
  </header>

  <div class="grid">
    <div class="stat-card">
      <div class="stat-label">Multi-User Architecture</div>
      <div class="stat-val" style="color: var(--success);">Isolated Silos</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Privacy Model</div>
      <div class="stat-val" style="color: var(--accent);">Zero Cross-Visibility</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Pairing Flow</div>
      <div class="stat-val" style="color: var(--success);">Private Link Key</div>
    </div>
  </div>

  <div class="tabs">
    <button class="tab-btn active" onclick="showTab('dashboard')">Interactive Testing Hub</button>
    <button class="tab-btn" onclick="showTab('extension-guide')">Chrome Extension Setup</button>
    <button class="tab-btn" onclick="showTab('app-guide')">Mobile App Setup</button>
    <button class="tab-btn" onclick="showTab('api')">API Endpoints</button>
  </div>

  <!-- DASHBOARD TAB -->
  <div id="tab-dashboard" class="tab-content active">
    <!-- Generator card -->
    <div class="card">
      <h2><span>🔑</span> Generate / Manage Link Code</h2>
      <p style="color: var(--text-muted); font-size: 0.9rem; margin-bottom: 1rem;">
        Generate a clean 6-digit code or look up an existing code to inspect connected devices or trigger test notifications.
      </p>
      <div class="input-row">
        <button onclick="generateCode()">Generate New Link Code</button>
        <input type="text" id="targetCode" placeholder="Enter 6-digit Code (e.g. 583920)" maxlength="12">
        <button class="secondary" onclick="inspectCode()">Check Status</button>
        <button class="secondary" onclick="sendTestPush()">Send Test Notification</button>
      </div>
      <div id="dash-log" class="output-box">// Output logs will appear here...</div>
    </div>

    <!-- Telemetry Simulator card -->
    <div class="card">
      <h2><span>🛫</span> Flight Telemetry Simulator</h2>
      <p style="color: var(--text-muted); font-size: 0.9rem; margin-bottom: 1rem;">
        Simulate the Chrome extension sending flight telemetry to a specific paired link code.
      </p>
      <div class="input-row">
        <input type="text" id="simOrigin" value="JFK" placeholder="Origin" style="max-width: 100px;">
        <input type="text" id="simDest" value="LAX" placeholder="Dest" style="max-width: 100px;">
        <input type="text" id="simPlane" value="Boeing 737-800" placeholder="Aircraft">
        <select id="simStage" style="max-width: 180px;">
          <option value="1">Takeoff (Stage 1)</option>
          <option value="2" selected>Ascent (Stage 2)</option>
          <option value="3">Cruise (Stage 3)</option>
          <option value="4">Descent (Stage 4)</option>
          <option value="5">Landed (Stage 5)</option>
        </select>
        <button onclick="sendSimulatedFlight()">Dispatch Flight Webhook</button>
      </div>
      <div id="sim-log" class="output-box">// Telemetry webhook response will appear here...</div>
    </div>
  </div>

  <!-- CHROME EXTENSION GUIDE TAB -->
  <div id="tab-extension-guide" class="tab-content">
    <div class="card">
      <h2><span>🧩</span> Linking the Chrome Extension in 2 Easy Steps</h2>
      
      <div class="step">
        <div><span class="step-num">1</span><strong>Save the User's Link Code in Chrome Extension Storage</strong></div>
        <p style="color: var(--text-muted); font-size: 0.9rem; margin: 0.4rem 0;">In your extension popup or options page, let the user enter the 6-digit link code shown on their phone:</p>
        <div class="code-block">// In popup.js:
const linkCode = document.getElementById('linkCodeInput').value.trim();
chrome.storage.sync.set({{ linkCode: linkCode }}, () => {{
  console.log("Saved Pocket Pilots link code:", linkCode);
}});</div>
      </div>

      <div class="step">
        <div><span class="step-num">2</span><strong>Send the Code with Flight Telemetry</strong></div>
        <p style="color: var(--text-muted); font-size: 0.9rem; margin: 0.4rem 0;">Simply add <code>?code=YOUR_CODE</code> to the webhook URL (or include <code>pair_code</code> in the JSON payload):</p>
        <div class="code-block">async function sendFlightUpdate(flightData) {{
  const {{ linkCode }} = await chrome.storage.sync.get(['linkCode']);
  const codeParam = linkCode ? `?code=${{encodeURIComponent(linkCode)}}` : '';

  const response = await fetch(`http://YOUR_SERVER_HOST:8000/webhook/flight${{codeParam}}`, {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{
      id: flightData.id,
      t0: flightData.t0,
      now: Date.now(),
      origin: flightData.origin,
      dest: flightData.dest,
      planeType: flightData.planeType,
      trk: flightData.trk
    }})
  }});
  return await response.json();
}}</div>
      </div>
    </div>
  </div>

  <!-- MOBILE APP GUIDE TAB -->
  <div id="tab-app-guide" class="tab-content">
    <div class="card">
      <h2><span>📱</span> Linking the Mobile App in 2 Easy Steps</h2>

      <div class="step">
        <div><span class="step-num">1</span><strong>Register the FCM Token</strong></div>
        <p style="color: var(--text-muted); font-size: 0.9rem; margin: 0.4rem 0;">When the app starts, fetch the FCM token and register with the server:</p>
        <div class="code-block">// Kotlin (Android):
FirebaseMessaging.getInstance().token.addOnCompleteListener {{ task ->
    if (!task.isSuccessful) return@addOnCompleteListener
    val token = task.result

    val json = JSONObject().apply {{
        put("token", token)
        put("device_id", "${{Build.MANUFACTURER}} ${{Build.MODEL}}")
        // Optional: pass existing pair_code if user entered one, otherwise omit to auto-generate
    }}

    // POST to http://YOUR_SERVER_HOST:8000/register-token
    // Server responds with:
    // {{ "status": "success", "pair_code": "583920", "devices_count": 1 }}
}}</div>
      </div>

      <div class="step">
        <div><span class="step-num">2</span><strong>Display the Link Code to the User</strong></div>
        <p style="color: var(--text-muted); font-size: 0.9rem; margin: 0.4rem 0;">Show the returned <code>pair_code</code> (e.g. <strong>583920</strong>) in the app settings or welcome screen:</p>
        <div class="code-block">"Your Link Code is: 583920"
"Enter this code in your Pocket Pilots Chrome extension to connect."</div>
        <p style="color: var(--text-muted); font-size: 0.85rem; margin-top: 0.5rem;">
          Want to link multiple devices (e.g. tablet + phone)? Just register the tablet with the same <code>pair_code: "583920"</code>! Both devices will receive flight notifications simultaneously.
        </p>
      </div>
    </div>
  </div>

  <!-- API REFERENCE TAB -->
  <div id="tab-api" class="tab-content">
    <div class="card">
      <h2><span>🔌</span> API Reference</h2>
      <ul style="list-style: none; display: flex; flex-direction: column; gap: 1rem;">
        <li>
          <strong>POST /register-token</strong><br>
          <span style="color: var(--text-muted); font-size: 0.85rem;">Registers an FCM token. If <code>pair_code</code> is omitted, auto-generates a new 6-digit code.</span>
          <div class="code-block">{{ "token": "...", "device_id": "Pixel_8", "pair_code": "583920" }}</div>
        </li>
        <li>
          <strong>POST /webhook/flight?code=583920</strong> (or <code>/webhook/flight/583920</code>)<br>
          <span style="color: var(--text-muted); font-size: 0.85rem;">Ingests flight telemetry and pushes FCM updates to all linked devices.</span>
          <div class="code-block">{{ "id": "FL100", "t0": 1730000000, "now": 1730000500, "origin": "JFK", "dest": "LAX", "planeType": "B738", "trk": [0,0,0,0] }}</div>
        </li>
        <li>
          <strong>GET /pair/{{code}}</strong><br>
          <span style="color: var(--text-muted); font-size: 0.85rem;">Checks connection status and device count for a code.</span>
        </li>
        <li>
          <strong>POST /pair/{{code}}/test</strong><br>
          <span style="color: var(--text-muted); font-size: 0.85rem;">Sends a test notification to verify FCM setup.</span>
        </li>
        <li>
          <strong>GET /status</strong><br>
          <span style="color: var(--text-muted); font-size: 0.85rem;">Global server health and session metrics. Pass <code>?code=583920</code> for session status.</span>
        </li>
      </ul>
    </div>
  </div>
</div>

<script>
function showTab(name) {{
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById('tab-' + name).classList.add('active');
}}

async function generateCode() {{
  const log = document.getElementById('dash-log');
  log.textContent = 'Generating code...';
  try {{
    const res = await fetch('/pair/new', {{ method: 'POST' }});
    const data = await res.json();
    document.getElementById('targetCode').value = data.pair_code;
    log.textContent = JSON.stringify(data, null, 2);
  }} catch (e) {{
    log.textContent = 'Error: ' + e.message;
  }}
}}

async function inspectCode() {{
  const code = document.getElementById('targetCode').value.trim();
  const log = document.getElementById('dash-log');
  if (!code) {{ alert('Please enter a code'); return; }}
  log.textContent = 'Checking ' + code + '...';
  try {{
    const res = await fetch('/pair/' + encodeURIComponent(code));
    const data = await res.json();
    log.textContent = JSON.stringify(data, null, 2);
  }} catch (e) {{
    log.textContent = 'Error: ' + e.message;
  }}
}}

async function sendTestPush() {{
  const code = document.getElementById('targetCode').value.trim();
  const log = document.getElementById('dash-log');
  if (!code) {{ alert('Please enter a code'); return; }}
  log.textContent = 'Dispatching test push to ' + code + '...';
  try {{
    const res = await fetch('/pair/' + encodeURIComponent(code) + '/test', {{ method: 'POST' }});
    const data = await res.json();
    log.textContent = JSON.stringify(data, null, 2);
  }} catch (e) {{
    log.textContent = 'Error: ' + e.message;
  }}
}}

async function sendSimulatedFlight() {{
  const code = document.getElementById('targetCode').value.trim();
  const log = document.getElementById('sim-log');
  const origin = document.getElementById('simOrigin').value.trim() || 'JFK';
  const dest = document.getElementById('simDest').value.trim() || 'LAX';
  const plane = document.getElementById('simPlane').value.trim() || 'B738';
  const stage = parseInt(document.getElementById('simStage').value);

  const now = Date.now();
  const totalDuration = 3600; // 1 hour flight
  let elapsed = 100;
  if (stage === 1) elapsed = 15;
  else if (stage === 2) elapsed = 300;
  else if (stage === 3) elapsed = 1800;
  else if (stage === 4) elapsed = 3300;
  else if (stage === 5) elapsed = 3600;

  const t0 = now - (elapsed * 1000);
  const trk = [0, 40.64, -73.77, 0, 900, 41.0, -78.0, 35000, 2700, 36.0, -115.0, 35000, 3600, 33.94, -118.4, 0];

  const payload = {{
    id: 'SIM_' + Math.floor(now / 1000),
    t0: t0,
    now: now,
    origin: origin,
    dest: dest,
    planeType: plane,
    trk: trk,
    pair_code: code || undefined
  }};

  log.textContent = 'Posting flight telemetry...';
  try {{
    const url = '/webhook/flight' + (code ? '?code=' + encodeURIComponent(code) : '');
    const res = await fetch(url, {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(payload)
    }});
    const data = await res.json();
    log.textContent = JSON.stringify(data, null, 2);
  }} catch (e) {{
    log.textContent = 'Error: ' + e.message;
  }}
}}
</script>
</body>
</html>"""
    return HTMLResponse(content=html_content)
