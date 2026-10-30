"""Per-client rate limiting for failed pairing-code lookups."""

from datetime import datetime

from fastapi import HTTPException, Request

FAILED_ATTEMPTS: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    # Forwarding headers are attacker-controlled unless a trusted proxy is
    # explicitly configured. Use the actual peer address to prevent spoofing.
    return request.client.host if request.client else "unknown"


def check_rate_limit(request: Request):
    """Reject clients with ten or more failed attempts in the last minute."""
    client_ip = _client_ip(request)
    now = datetime.now().timestamp()
    attempts = [attempt for attempt in FAILED_ATTEMPTS.get(client_ip, []) if now - attempt < 60]
    FAILED_ATTEMPTS[client_ip] = attempts
    if len(attempts) >= 10:
        raise HTTPException(
            status_code=429,
            detail="Too many invalid attempts. Please wait 60 seconds before trying again.",
        )


def record_failed_attempt(request: Request):
    """Record a failed lookup against the caller's IP address."""
    FAILED_ATTEMPTS.setdefault(_client_ip(request), []).append(datetime.now().timestamp())
