"""In-memory pairing registry and pairing-code operations."""

import secrets

from pocketpilots import config, persistence
from pocketpilots.models import FlightSession

SESSIONS: dict[str, FlightSession] = {}


def save_sessions(session: FlightSession | None = None):
    """Persist one session or the full registry."""
    persistence.save_sessions(SESSIONS, session)


def load_sessions():
    """Load persisted session state into the shared registry."""
    persistence.load_sessions(SESSIONS)


def generate_unique_pair_code() -> str:
    """Generate a human-readable 80-bit bearer credential."""
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    for _ in range(1000):
        code = "".join(secrets.choice(alphabet) for _ in range(16))
        if code not in SESSIONS:
            code = "-".join(code[index:index + 4] for index in range(0, 16, 4))
            return code
    raise RuntimeError("Unable to generate a unique pairing code")


def get_or_create_session(pair_code: str) -> FlightSession:
    normalized_code = pair_code.strip().replace(" ", "").replace("-", "").upper()
    if normalized_code not in SESSIONS:
        SESSIONS[normalized_code] = FlightSession(normalized_code)
    return SESSIONS[normalized_code]


def get_registered_session(pair_code: str) -> FlightSession | None:
    """Resolve a session only when its bearer code meets the entropy policy."""
    normalized_code = pair_code.strip().replace(" ", "").replace("-", "").upper()
    if not normalized_code:
        return None
    if not config.ALLOW_LEGACY_PAIR_CODES and len(normalized_code) < 16:
        return None
    return SESSIONS.get(normalized_code)


def resolve_session(
    path_code: str | None = None,
    query_code: str | None = None,
    query_pair_code: str | None = None,
    header_code: str | None = None,
    body_pair_code: str | None = None,
    body_code: str | None = None,
) -> FlightSession | None:
    """Resolve only an explicitly supplied registered session code."""
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
    return get_registered_session(candidate)


load_sessions()
