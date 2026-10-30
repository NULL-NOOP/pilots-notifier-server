"""SQLite persistence and legacy JSON migration."""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from pocketpilots.config import DB_PATH, LOCAL_TZ, SESSIONS_FILE
from pocketpilots.models import FlightSession


@contextmanager
def get_db():
    """Provide a managed SQLite connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    """Create persistence tables and apply additive migrations."""
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
                for column, column_type in [
                    ("t0", "REAL"),
                    ("total_sec", "REAL"),
                    ("arrival_sec", "REAL"),
                    ("bounds_json", "TEXT"),
                    ("segments_json", "TEXT"),
                ]:
                    try:
                        conn.execute(f"ALTER TABLE active_flights ADD COLUMN {column} {column_type};")
                    except Exception:
                        pass
    except Exception as exc:
        print(f"Error initializing database {DB_PATH}: {exc}")


def db_save_session(session: FlightSession):
    """Save one session and its devices/active flight to SQLite."""
    try:
        with get_db() as conn:
            with conn:
                conn.execute("PRAGMA foreign_keys=ON;")
                conn.execute("""
                    INSERT INTO sessions (pair_code, created_at, last_seen)
                    VALUES (?, ?, ?)
                    ON CONFLICT(pair_code) DO UPDATE SET last_seen=excluded.last_seen;
                """, (session.pair_code, session.created_at.isoformat(), session.last_seen.isoformat()))

                device_ids = list(session.devices)
                if device_ids:
                    placeholders = ",".join("?" * len(device_ids))
                    conn.execute(
                        f"DELETE FROM devices WHERE pair_code=? AND device_id NOT IN ({placeholders})",
                        [session.pair_code] + device_ids,
                    )
                else:
                    conn.execute("DELETE FROM devices WHERE pair_code=?", (session.pair_code,))

                now_iso = datetime.now(LOCAL_TZ).isoformat()
                for device_id, token in session.devices.items():
                    conn.execute("""
                        INSERT INTO devices (pair_code, device_id, token, updated_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(pair_code, device_id) DO UPDATE
                        SET token=excluded.token, updated_at=excluded.updated_at;
                    """, (session.pair_code, device_id, token, now_iso))

                flight = session.current_flight
                if flight.get("id"):
                    conn.execute("""
                        INSERT INTO active_flights (
                            pair_code, flight_id, route, plane_type, t0, total_sec,
                            arrival_sec, bounds_json, segments_json, last_status,
                            phase, progress, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        flight.get("id"),
                        flight.get("route"),
                        flight.get("plane_type"),
                        flight.get("t0"),
                        flight.get("total_sec"),
                        flight.get("arrival_sec"),
                        json.dumps(flight.get("bounds")) if flight.get("bounds") else None,
                        json.dumps(flight.get("segments")) if flight.get("segments") else None,
                        flight.get("last_status"),
                        flight.get("phase"),
                        flight.get("progress", 0),
                        flight.get("updated_at") or now_iso,
                    ))
    except Exception as exc:
        print(f"Error saving session {session.pair_code} to DB: {exc}")


def save_sessions(sessions: dict[str, FlightSession], session: FlightSession | None = None):
    """Persist sessions to SQLite and mirror the in-memory registry to JSON."""
    if session:
        db_save_session(session)
    else:
        for current_session in list(sessions.values()):
            db_save_session(current_session)

    try:
        data = {}
        for code, current_session in sessions.items():
            if current_session.devices:
                data[code] = {
                    "pair_code": current_session.pair_code,
                    "devices": current_session.devices,
                    "created_at": current_session.created_at.isoformat(),
                    "last_seen": current_session.last_seen.isoformat(),
                    "current_flight": (
                        current_session.current_flight
                        if current_session.current_flight.get("id")
                        else None
                    ),
                }
        temp_file = f"{SESSIONS_FILE}.tmp"
        with open(temp_file, "w") as output:
            json.dump(data, output, indent=2)
        os.replace(temp_file, SESSIONS_FILE)
    except Exception as exc:
        print(f"Error mirroring sessions to {SESSIONS_FILE}: {exc}")


def load_sessions(sessions: dict[str, FlightSession]):
    """Load persisted sessions, falling back to and migrating the legacy JSON file."""
    loaded_from_db = False
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT * FROM sessions").fetchall()
            for row in rows:
                code = row["pair_code"].strip().replace(" ", "").upper()
                session = FlightSession(code)
                try:
                    session.created_at = datetime.fromisoformat(row["created_at"])
                    session.last_seen = datetime.fromisoformat(row["last_seen"])
                except Exception:
                    pass

                devices = conn.execute(
                    "SELECT * FROM devices WHERE pair_code=?", (code,)
                ).fetchall()
                for device in devices:
                    session.devices[device["device_id"]] = device["token"]

                flight_row = conn.execute(
                    "SELECT * FROM active_flights WHERE pair_code=?", (code,)
                ).fetchone()
                if flight_row and flight_row["flight_id"]:
                    try:
                        bounds = json.loads(flight_row["bounds_json"]) if flight_row["bounds_json"] else None
                    except Exception:
                        bounds = None
                    try:
                        segments = json.loads(flight_row["segments_json"]) if flight_row["segments_json"] else None
                    except Exception:
                        segments = None
                    session.current_flight = {
                        "id": flight_row["flight_id"],
                        "route": flight_row["route"],
                        "plane_type": flight_row["plane_type"],
                        "t0": flight_row["t0"] if "t0" in flight_row.keys() else None,
                        "total_sec": flight_row["total_sec"] if "total_sec" in flight_row.keys() else None,
                        "arrival_sec": flight_row["arrival_sec"] if "arrival_sec" in flight_row.keys() else None,
                        "bounds": bounds,
                        "segments": segments,
                        "last_status": flight_row["last_status"],
                        "phase": flight_row["phase"],
                        "progress": flight_row["progress"],
                        "updated_at": flight_row["updated_at"],
                    }
                sessions[code] = session
                loaded_from_db = True

        if loaded_from_db and sessions:
            print(
                f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] Loaded {len(sessions)} "
                f"session(s) from persistent database ({DB_PATH})"
            )
            return
    except Exception as exc:
        print(f"Database load error ({DB_PATH}): {exc}")

    if os.path.exists(SESSIONS_FILE):
        try:
            with open(SESSIONS_FILE, "r") as source:
                data = json.load(source)
            for code, item in data.items():
                normalized_code = code.strip().replace(" ", "").upper()
                session = FlightSession(normalized_code)
                session.devices = item.get("devices", {})
                try:
                    session.created_at = datetime.fromisoformat(item["created_at"])
                    session.last_seen = datetime.fromisoformat(item["last_seen"])
                except Exception:
                    pass
                if item.get("current_flight"):
                    session.current_flight = item["current_flight"]
                sessions[normalized_code] = session
                db_save_session(session)
            print(
                f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] Migrated {len(sessions)} "
                f"session(s) from {SESSIONS_FILE} into database"
            )
        except Exception as exc:
            print(f"Error loading fallback sessions from {SESSIONS_FILE}: {exc}")


init_db()
