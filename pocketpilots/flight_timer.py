"""Background flight phase notifications and restart recovery."""

import asyncio
import time
from datetime import datetime, timedelta

from pocketpilots.config import LOCAL_TZ
from pocketpilots.models import FlightSession
from pocketpilots.notifications import send_fcm_notification
from pocketpilots.sessions import SESSIONS, save_sessions
from pocketpilots.telemetry import get_stage_from_bounds


async def handle_flight_timer(
    session: FlightSession,
    flight_id: str,
    route: str,
    total_sec: float,
    elapsed_sec: float,
    arrival_sec: float,
    plane_type: str,
    bounds: dict,
    segments: list[int],
):
    """Step through flight phase transitions using wall-clock time."""
    try:
        start_wall_time = time.time()
        initial_elapsed = elapsed_sec
        while True:
            await asyncio.sleep(5.0)
            if session.current_flight.get("id") != flight_id:
                break

            current_elapsed = min(total_sec, initial_elapsed + (time.time() - start_wall_time))
            status_code, phase_label = get_stage_from_bounds(current_elapsed, total_sec, bounds)
            progress_pct = min(100.0, current_elapsed / total_sec * 100.0) if total_sec > 0 else 100.0
            if status_code == 5:
                progress_pct = 100.0

            if session.current_flight.get("last_status") != status_code:
                session.current_flight["last_status"] = status_code
                session.current_flight["phase"] = phase_label
                session.current_flight["progress"] = int(progress_pct)
                session.current_flight["updated_at"] = datetime.now(LOCAL_TZ).isoformat()
                save_sessions(session)

                remaining_sec = max(0.0, total_sec - current_elapsed)
                arrival_dt = datetime.now(LOCAL_TZ) + timedelta(seconds=remaining_sec)
                arrival_str = arrival_dt.strftime("%I:%M %p").lstrip("0")
                tokens = session.fcm_tokens
                if status_code == 5:
                    destination = route.split("➔")[1].strip() if "➔" in route else "destination"
                    await send_fcm_notification(
                        title=f"{route} - Landed!",
                        message=f"Touchdown at {destination} ({plane_type})",
                        status_code=5,
                        phase_label="Landed",
                        segments=segments,
                        route=route,
                        progress=100,
                        tokens=tokens,
                        session=session,
                    )
                    break

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
                    session=session,
                )

            if current_elapsed >= total_sec:
                break
    except asyncio.CancelledError:
        pass


def resume_active_flight_timers():
    """Resume persisted in-progress flights after a server restart."""
    now = time.time()
    for session in SESSIONS.values():
        flight = session.current_flight
        if flight and flight.get("id") and flight.get("last_status") != 5:
            t0 = flight.get("t0")
            total_sec = flight.get("total_sec")
            bounds = flight.get("bounds")
            segments = flight.get("segments")
            if t0 and total_sec and bounds and segments:
                elapsed_sec = max(0.0, now - t0)
                if elapsed_sec < total_sec + 120:
                    print(
                        f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] "
                        f"[{session.pair_code}] Resuming in-progress flight timer: "
                        f"{flight.get('id')} ({flight.get('route')}). "
                        f"Elapsed: {int(elapsed_sec)}s / {int(total_sec)}s"
                    )
                    session.cancel_task()
                    session.flight_task = asyncio.create_task(
                        handle_flight_timer(
                            session=session,
                            flight_id=flight["id"],
                            route=flight["route"],
                            total_sec=total_sec,
                            elapsed_sec=elapsed_sec,
                            arrival_sec=flight.get("arrival_sec", t0 + total_sec),
                            plane_type=flight.get("plane_type", "Aircraft"),
                            bounds=bounds,
                            segments=segments,
                        )
                    )
