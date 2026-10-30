"""Firebase Cloud Messaging delivery and stale-token cleanup."""

import asyncio
from datetime import datetime

from firebase_admin import messaging

from pocketpilots import config
from pocketpilots.config import LOCAL_TZ
from pocketpilots.models import FlightSession
from pocketpilots.sessions import save_sessions


async def send_fcm_notification(
    title: str,
    message: str,
    status_code: int,
    phase_label: str,
    segments: list[int],
    route: str = "",
    progress: int = 0,
    arrival_sec: float | None = None,
    tokens: list[str] | None = None,
    session: FlightSession | None = None,
) -> dict:
    """Dispatch a notification to explicit tokens or every device in a session."""
    if tokens is None:
        if session:
            tokens = session.fcm_tokens
        else:
            print(
                f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] "
                "Push rejected: No session or target tokens provided."
            )
            return {"sent": 0, "failed": 0, "total": 0}

    code_label = session.pair_code if session else "GLOBAL"
    if not tokens:
        print(
            f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] "
            f"[{code_label}] No FCM tokens registered. Skipping push."
        )
        return {"sent": 0, "failed": 0, "total": 0}

    data_payload = {
        "title": title,
        "message": message,
        "route": route,
        "progress": str(progress),
        "arrival_sec": str(arrival_sec) if arrival_sec else "0",
        "status": str(status_code),
        "phase": phase_label,
        "segments": ",".join(map(str, segments)),
    }

    if not config.FIREBASE_INITIALIZED:
        print(
            f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] [{code_label}] "
            f"[SIMULATION] Push to {len(tokens)} device(s) ({phase_label} | Code {status_code}): "
            f"{title} - {message}"
        )
        return {"sent": len(tokens), "failed": 0, "total": len(tokens)}

    def send_one(token: str):
        notification = messaging.Message(
            token=token,
            android=messaging.AndroidConfig(priority="high", ttl=0),
            data=data_payload,
        )
        return messaging.send(notification)

    sent = 0
    failed = 0
    stale_tokens = []
    for token in tokens:
        try:
            response = await asyncio.to_thread(send_one, token)
            sent += 1
            print(
                f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] [{code_label}] "
                f"FCM Push Sent to {token[:12]}... ({phase_label} | Code {status_code}): {response}"
            )
        except messaging.UnregisteredError:
            failed += 1
            stale_tokens.append(token)
            print(
                f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] [{code_label}] "
                f"FCM Token Unregistered (pruning): {token[:12]}..."
            )
        except Exception as exc:
            failed += 1
            print(
                f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] [{code_label}] "
                f"FCM Push Error for {token[:12]}...: {exc}"
            )

    if stale_tokens and session:
        for token in stale_tokens:
            session.remove_token(token)
        save_sessions()

    return {"sent": sent, "failed": failed, "total": len(tokens)}
