"""Domain and API request models."""

import asyncio
from datetime import datetime

from pydantic import BaseModel

from pocketpilots.config import LOCAL_TZ


class FlightSession:
    """Manages active devices and flight status for a single pairing code."""

    def __init__(self, pair_code: str):
        self.pair_code: str = pair_code.strip().replace(" ", "").upper()
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
        self.created_at = datetime.now(LOCAL_TZ)
        self.last_seen = datetime.now(LOCAL_TZ)

    def add_token(self, device_id: str, token: str):
        self.last_seen = datetime.now(LOCAL_TZ)
        device_id = device_id.strip() if device_id else "android_main"

        for existing_id, existing_token in list(self.devices.items()):
            if existing_token == token and existing_id != device_id:
                del self.devices[existing_id]

        if (
            device_id == "android_main"
            and device_id in self.devices
            and self.devices[device_id] != token
        ):
            index = 2
            while f"android_main_{index}" in self.devices:
                index += 1
            device_id = f"android_main_{index}"

        self.devices[device_id] = token

    def remove_device(self, device_id: str):
        self.last_seen = datetime.now(LOCAL_TZ)
        self.devices.pop(device_id, None)

    def remove_token(self, token: str):
        self.last_seen = datetime.now(LOCAL_TZ)
        stale_devices = [device for device, value in self.devices.items() if value == token]
        for device in stale_devices:
            del self.devices[device]

    @property
    def fcm_tokens(self) -> list[str]:
        return list(dict.fromkeys(self.devices.values()))

    def cancel_task(self):
        if self.flight_task and not self.flight_task.done():
            self.flight_task.cancel()
        self.flight_task = None


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


class FlightCancellation(BaseModel):
    id: str | None = None
    pair_code: str | None = None
    code: str | None = None
