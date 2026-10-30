"""HTTP API endpoints for the notification server."""

import asyncio
from datetime import datetime, timedelta

from fastapi import APIRouter, Header, HTTPException, Query, Request

from pocketpilots import config
from pocketpilots.config import LOCAL_TZ
from pocketpilots.flight_timer import handle_flight_timer
from pocketpilots.models import (FlightCancellation, FlightPayload,
                                 TokenRegistration, TokenUnregistration)
from pocketpilots.notifications import send_fcm_notification
from pocketpilots.security import check_rate_limit, record_failed_attempt
from pocketpilots.sessions import (generate_unique_pair_code,
                                   get_or_create_session,
                                   get_registered_session, resolve_session,
                                   save_sessions)
from pocketpilots.telemetry import (analyze_flight_profile,
                                    get_stage_from_bounds)

router = APIRouter()


@router.post("/webhook/flight/cancel")
async def cancel_flight(
    payload: FlightCancellation,
    request: Request,
    code: str | None = Query(default=None),
    pair_code: str | None = Query(default=None),
    x_pair_code: str | None = Header(default=None, alias="X-Pair-Code"),
    x_code: str | None = Header(default=None, alias="X-Code"),
):
    check_rate_limit(request)
    session = resolve_session(
        query_code=code,
        query_pair_code=pair_code,
        header_code=x_pair_code or x_code,
        body_pair_code=payload.pair_code,
        body_code=payload.code,
    )
    if not session:
        record_failed_attempt(request)
        raise HTTPException(status_code=401, detail="Missing or invalid link code.")

    flight = session.current_flight
    flight_id = flight.get("id")
    if not flight_id or (payload.id and payload.id != flight_id):
        raise HTTPException(status_code=409, detail="No matching active trip to cancel.")

    session.cancel_task()
    route = flight.get("route") or "Flight"
    phase_status = flight.get("last_status")
    status_code = phase_status if phase_status in (1, 2, 3, 4, 5) else 1
    progress = int(flight.get("progress", 0))
    segments = flight.get("segments") or [5, 22, 46, 22, 5]
    flight.update({
        "id": None,
        "phase": "Canceled",
        "updated_at": datetime.now(LOCAL_TZ).isoformat(),
    })
    save_sessions(session)

    result = await send_fcm_notification(
        title=f"{route} - Canceled",
        message="Trip canceled",
        status_code=status_code,
        phase_label="Canceled",
        segments=segments,
        route=route,
        progress=progress,
        tokens=session.fcm_tokens,
        session=session,
    )
    return {
        "status": "Trip canceled",
        "pair_code": session.pair_code,
        "devices_notified": result["sent"],
        "flight_id": flight_id,
    }


@router.post("/webhook/flight")
@router.post("/webhook/flight/{path_pair_code}")
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
            detail="Missing or invalid link code. Please provide a registered pair code in the URL, header, or body.",
        )

    t0_sec = payload.t0 / 1000.0 if payload.t0 > 1e11 else payload.t0
    now_sec = payload.now / 1000.0 if payload.now > 1e11 else payload.now
    total_sec = payload.trk[-4] if len(payload.trk) >= 4 else 0.0
    elapsed_sec = max(0.0, now_sec - t0_sec)
    remaining_sec = max(0.0, total_sec - elapsed_sec)
    arrival_sec = now_sec + remaining_sec
    progress_pct = min(100.0, elapsed_sec / total_sec * 100.0) if total_sec > 0 else 100.0

    bounds, segments = analyze_flight_profile(payload.trk, total_sec)
    status_code, phase_label = get_stage_from_bounds(elapsed_sec, total_sec, bounds)
    if status_code == 5:
        progress_pct = 100.0
    route_str = f"{payload.origin} ➔ {payload.dest}"

    if session.current_flight.get("id") != payload.id:
        session.current_flight["id"] = payload.id
        session.current_flight["last_status"] = None
    session.current_flight.update({
        "last_status": status_code,
        "route": route_str,
        "phase": phase_label,
        "plane_type": payload.planeType,
        "progress": int(progress_pct),
        "t0": t0_sec,
        "total_sec": total_sec,
        "arrival_sec": arrival_sec,
        "bounds": bounds,
        "segments": segments,
        "updated_at": datetime.now(LOCAL_TZ).isoformat(),
    })
    save_sessions(session)

    arrival_dt = datetime.now(LOCAL_TZ) + timedelta(seconds=remaining_sec)
    arrival_str = arrival_dt.strftime("%I:%M %p").lstrip("0")
    tokens = session.fcm_tokens
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
            session=session,
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
            session=session,
        )

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
            segments,
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
        "remaining_sec": round(remaining_sec, 1),
    }


@router.post("/register-token")
async def register_token(data: TokenRegistration, request: Request):
    check_rate_limit(request)
    raw_code = data.pair_code or data.code
    is_new = False
    if raw_code and raw_code.strip():
        pair_code = raw_code.strip().replace(" ", "").replace("-", "").upper()
        session = get_registered_session(pair_code)
        if not session:
            record_failed_attempt(request)
            raise HTTPException(status_code=404, detail="Pair code not found. Generate a code before linking devices.")
    else:
        pair_code = generate_unique_pair_code()
        is_new = True
        session = get_or_create_session(pair_code)

    session.add_token(data.device_id, data.token)
    save_sessions(session)
    print(
        f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] Registered device "
        f"'{data.device_id}' to pair code [{pair_code}]. Total devices: {len(session.devices)}"
    )
    return {
        "status": "success",
        "pair_code": pair_code,
        "is_new_code": is_new,
        "device_id": data.device_id,
        "devices_count": len(session.devices),
        "devices": list(session.devices.keys()),
        "message": f"Device '{data.device_id}' linked to pair code {pair_code}",
    }


@router.post("/unregister-token")
async def unregister_token(data: TokenUnregistration, request: Request):
    check_rate_limit(request)
    raw_code = data.pair_code or data.code
    if not raw_code or not raw_code.strip():
        raise HTTPException(status_code=400, detail="A pair_code is required to scope token removal.")
    if not data.device_id and not data.token:
        raise HTTPException(status_code=400, detail="A device_id or token is required.")
    normalized_code = raw_code.strip().replace(" ", "").replace("-", "").upper()
    session = get_registered_session(normalized_code)
    if not session:
        record_failed_attempt(request)
        raise HTTPException(status_code=404, detail="Pair code not found.")

    removed = 0
    if data.device_id and data.device_id in session.devices:
        session.remove_device(data.device_id)
        removed += 1
    elif data.token:
        before = len(session.devices)
        session.remove_token(data.token)
        removed += before - len(session.devices)

    save_sessions()
    return {"status": "success", "removed_count": removed}


@router.post("/pair/new")
@router.get("/pair/new")
async def create_new_pair():
    code = generate_unique_pair_code()
    get_or_create_session(code)
    save_sessions()
    return {
        "status": "success",
        "pair_code": code,
        "message": f"Pair code {code} generated. Enter this in your app or Chrome extension to link.",
    }


@router.get("/pair/{pair_code}")
async def get_pair_status(pair_code: str, request: Request):
    check_rate_limit(request)
    normalized_code = pair_code.strip().replace(" ", "").replace("-", "").upper()
    session = get_registered_session(normalized_code)
    if not session:
        record_failed_attempt(request)
        raise HTTPException(status_code=404, detail="Pair code not found.")
    return {
        "exists": True,
        "pair_code": normalized_code,
        "device_count": len(session.devices),
        "devices": list(session.devices.keys()),
        "fcm_token_registered": len(session.devices) > 0,
        "has_active_flight": bool(session.current_flight.get("id")),
        "active_flight": session.current_flight,
        "last_seen": session.last_seen.isoformat(),
    }


@router.post("/pair/{pair_code}/test")
async def send_test_push(pair_code: str, request: Request):
    check_rate_limit(request)
    normalized_code = pair_code.strip().replace(" ", "").replace("-", "").upper()
    session = get_registered_session(normalized_code)
    if not session:
        record_failed_attempt(request)
        raise HTTPException(status_code=404, detail=f"Pair code '{normalized_code}' not found.")
    if not session.fcm_tokens:
        raise HTTPException(
            status_code=400,
            detail=f"No devices / FCM tokens registered under code '{normalized_code}'.",
        )

    result = await send_fcm_notification(
        title="Pocket Pilots Connected! ✈️",
        message="Your device is successfully linked to Pocket Pilots.",
        status_code=1,
        phase_label="Linked",
        segments=[5, 20, 50, 20, 5],
        route="Extension ➔ App",
        progress=100,
        tokens=session.fcm_tokens,
        session=session,
    )
    return {
        "status": "success",
        "pair_code": normalized_code,
        "message": f"Dispatched test push to {result['sent']} device(s).",
        "results": result,
    }


@router.get("/status")
async def get_status(
    request: Request,
    code: str | None = Query(default=None),
    pair_code: str | None = Query(default=None),
    x_pair_code: str | None = Header(default=None, alias="X-Pair-Code"),
):
    target_code = pair_code or code or x_pair_code
    if target_code:
        check_rate_limit(request)
        normalized_code = target_code.strip().replace(" ", "").replace("-", "").upper()
        session = get_registered_session(normalized_code)
        if not session:
            record_failed_attempt(request)
            raise HTTPException(status_code=404, detail=f"Pair code '{normalized_code}' not found.")
        return {
            "status": "ok",
            "pair_code": normalized_code,
            "fcm_token_registered": len(session.devices) > 0,
            "device_count": len(session.devices),
            "devices": list(session.devices.keys()),
            "active_flight": session.current_flight,
        }

    return {
        "status": "ok",
        "service": "Pocket Pilots Notification Hub",
        "firebase_live": config.FIREBASE_INITIALIZED,
        "version": "2.1.0",
    }
