import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import firebase_admin
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from firebase_admin import credentials, messaging
from pydantic import BaseModel

cred = credentials.Certificate("serviceAccountKey.json")
firebase_admin.initialize_app(cred)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

LOCAL_TZ = ZoneInfo("America/New_York")
ACTIVE_FCM_TOKEN = None
current_flight_task = None

current_flight = {
    "id": None,
    "last_status": None
}

class TokenRegistration(BaseModel):
    token: str
    device_id: str = "android_main"

class FlightPayload(BaseModel):
    id: str
    t0: float
    now: float
    origin: str
    dest: str
    planeType: str
    trk: list[float]


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
    arrival_sec: float = None
):
    global ACTIVE_FCM_TOKEN
    
    if not ACTIVE_FCM_TOKEN:
        print(f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] No FCM token registered. Skipping push.")
        return

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

    fcm_message = messaging.Message(
        token=ACTIVE_FCM_TOKEN,
        android=messaging.AndroidConfig(priority="high", ttl=0),
        data=data_payload
    )

    try:
        response = await asyncio.to_thread(messaging.send, fcm_message)
        print(f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] FCM Push Sent ({phase_label} | Code {status_code} | Segments {segments}): {response}")
    except Exception as e:
        print(f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] FCM Push Error: {e}")


async def handle_flight_timer(
    flight_id: str, 
    route: str, 
    total_sec: float, 
    elapsed_sec: float, 
    arrival_sec: float, 
    plane_type: str,
    bounds: dict,
    segments: list[int]
):
    """Background task that steps through phase transitions if browser webhooks stop."""
    try:
        curr_elapsed = elapsed_sec
        while curr_elapsed < total_sec:
            await asyncio.sleep(5.0)  # Check progress every 5 seconds
            curr_elapsed += 5.0
            
            status_code, phase_label = get_stage_from_bounds(curr_elapsed, total_sec, bounds)
            progress_pct = (curr_elapsed / total_sec * 100.0) if total_sec > 0 else 100.0

            if current_flight["last_status"] != status_code:
                current_flight["last_status"] = status_code
                
                remaining_sec = max(0.0, total_sec - curr_elapsed)
                arrival_dt = datetime.now(LOCAL_TZ) + timedelta(seconds=remaining_sec)
                arrival_str = arrival_dt.strftime('%I:%M %p').lstrip('0')

                if status_code == 5:
                    dest = route.split('➔')[1].strip() if '➔' in route else "destination"
                    await send_fcm_notification(
                        title=f"{route} - Landed!",
                        message=f"Touchdown at {dest} ({plane_type})",
                        status_code=5,
                        phase_label="Landed",
                        segments=segments,
                        route=route,
                        progress=100
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
                        arrival_sec=arrival_sec
                    )

    except asyncio.CancelledError:
        pass


@app.post("/webhook/flight")
async def receive_flight_update(payload: FlightPayload):
    global current_flight_task, current_flight
    
    t0_sec = payload.t0 / 1000.0 if payload.t0 > 1e11 else payload.t0
    now_sec = payload.now / 1000.0 if payload.now > 1e11 else payload.now
    total_sec = payload.trk[-4] if len(payload.trk) >= 4 else 0.0
    
    elapsed_sec = max(0.0, now_sec - t0_sec)
    remaining_sec = max(0.0, total_sec - elapsed_sec)
    arrival_sec = now_sec + remaining_sec
    progress_pct = (elapsed_sec / total_sec * 100.0) if total_sec > 0 else 100.0

    bounds, segments = analyze_flight_profile(payload.trk, total_sec)
    status_code, phase_label = get_stage_from_bounds(elapsed_sec, total_sec, bounds)
    
    route_str = f"{payload.origin} ➔ {payload.dest}"

    if current_flight["id"] != payload.id:
        current_flight["id"] = payload.id
        current_flight["last_status"] = None

    current_flight["last_status"] = status_code

    arrival_dt = datetime.now(LOCAL_TZ) + timedelta(seconds=remaining_sec)
    arrival_str = arrival_dt.strftime('%I:%M %p').lstrip('0')

    # Send notification for incoming webhook payload
    if status_code == 5:
        await send_fcm_notification(
            title=f"{route_str} - Landed!",
            message=f"Touchdown at {payload.dest} ({payload.planeType})",
            status_code=5,
            phase_label="Landed",
            segments=segments,
            route=route_str,
            progress=100
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
            arrival_sec=arrival_sec
        )

    # Cancel previous background timer and start a new one
    if current_flight_task and not current_flight_task.done():
        current_flight_task.cancel()
    
    current_flight_task = asyncio.create_task(
        handle_flight_timer(
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
        "phase": phase_label,
        "status_code": status_code,
        "segments": segments,
        "progress_pct": round(progress_pct, 1),
        "remaining_sec": round(remaining_sec, 1)
    }


@app.post("/register-token")
async def register_token(data: TokenRegistration):
    global ACTIVE_FCM_TOKEN
    ACTIVE_FCM_TOKEN = data.token
    print(f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] Registered FCM token: {ACTIVE_FCM_TOKEN[:15]}...")
    return {"status": "success", "message": "FCM Token registered"}


@app.get("/status")
async def get_status():
    global ACTIVE_FCM_TOKEN
    return {
        "status": "ok",
        "fcm_token_registered": bool(ACTIVE_FCM_TOKEN),
        "active_flight": current_flight
    }