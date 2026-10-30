"""ASGI application entry point and backwards-compatible exports."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from pocketpilots import config
from pocketpilots.config import (DB_PATH, FIREBASE_INITIALIZED, LOCAL_TZ,
                                 SERVICE_ACCOUNT_PATH, SESSIONS_FILE,
                                 credentials, firebase_admin, messaging)
from pocketpilots.dashboard import get_dashboard
from pocketpilots.flight_timer import (handle_flight_timer,
                                       resume_active_flight_timers)
from pocketpilots.models import (FlightCancellation, FlightPayload,
                                 FlightSession, TokenRegistration,
                                 TokenUnregistration)
from pocketpilots.notifications import send_fcm_notification
from pocketpilots.persistence import db_save_session, get_db, init_db
from pocketpilots.routes import (cancel_flight, create_new_pair,
                                 get_pair_status, get_status,
                                 receive_flight_update, register_token, router,
                                 send_test_push, unregister_token)
from pocketpilots.security import (FAILED_ATTEMPTS, check_rate_limit,
                                   record_failed_attempt)
from pocketpilots.sessions import (SESSIONS, generate_unique_pair_code,
                                   get_or_create_session, load_sessions,
                                   resolve_session, save_sessions)
from pocketpilots.telemetry import (analyze_flight_profile,
                                    get_stage_from_bounds)

app = FastAPI(
    title="Pocket Pilots Notification Server",
    description="Multi-device, multi-user pairing and FCM notification backend for Pocket Pilots",
    version="2.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
app.add_api_route("/", get_dashboard, methods=["GET"], response_class=HTMLResponse)
app.add_api_route("/pair", get_dashboard, methods=["GET"], response_class=HTMLResponse)


@app.on_event("startup")
async def startup_event():
    resume_active_flight_timers()
