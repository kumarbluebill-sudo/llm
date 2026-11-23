"""AviationStack client: live flight status/schedule/delay (NOT fares or booking).

Used by the DETECT stage to poll flights we have booked. The free plan allows only ~100 requests
a month, so the client enforces a monthly budget and caches results; HTTPS only, and the API key
(a query parameter) never appears in errors or logs."""
import os
import time
import urllib.parse
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from mcp_servers.http import HttpError, Transport, urllib_transport

BASE = "https://api.aviationstack.com/v1"


class AviationStackError(Exception):
    pass


class QuotaExceeded(AviationStackError):
    pass


class FlightStatus(BaseModel):
    flight_iata: str
    status: str  # scheduled | active | landed | cancelled | incident | diverted
    airline: str | None = None
    dep_iata: str | None = None
    arr_iata: str | None = None
    scheduled_departure: datetime | None = None
    scheduled_arrival: datetime | None = None
    departure_delay_min: int = 0
    arrival_delay_min: int = 0

    @property
    def delay_min(self) -> int:
        return max(self.departure_delay_min, self.arrival_delay_min)


def _dt(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def parse_flight(rec: dict) -> FlightStatus | None:
    iata = (rec.get("flight") or {}).get("iata")
    if not iata or not rec.get("flight_status"):
        return None
    dep, arr = rec.get("departure") or {}, rec.get("arrival") or {}
    return FlightStatus(
        flight_iata=iata,
        status=rec["flight_status"],
        airline=(rec.get("airline") or {}).get("name"),
        dep_iata=dep.get("iata"),
        arr_iata=arr.get("iata"),
        scheduled_departure=_dt(dep.get("scheduled")),
        scheduled_arrival=_dt(arr.get("scheduled")),
        departure_delay_min=int(dep.get("delay") or 0),
        arrival_delay_min=int(arr.get("delay") or 0),
    )


class AviationStackClient:
    def __init__(
        self,
        api_key: str,
        transport: Transport = urllib_transport,
        monthly_budget: int = 80,
        cache_ttl: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
        month: Callable[[], str] = lambda: datetime.now(UTC).strftime("%Y-%m"),
    ) -> None:
        self._key, self._transport = api_key, transport
        self._budget, self._ttl, self._clock, self._month = monthly_budget, cache_ttl, clock, month
        self._used: dict[str, int] = {}
        self._cache: dict[str, tuple[float, FlightStatus | None]] = {}

    @classmethod
    def from_env(cls, **kw) -> "AviationStackClient | None":
        key = os.getenv("AVIATIONSTACK_API")
        return cls(key, **kw) if key else None

    @property
    def calls_this_month(self) -> int:
        return self._used.get(self._month(), 0)

    def flight_status(self, flight_iata: str) -> FlightStatus | None:
        hit = self._cache.get(flight_iata)
        if hit and self._clock() - hit[0] < self._ttl:
            return hit[1]
        month = self._month()
        if self._used.get(month, 0) >= self._budget:
            raise QuotaExceeded(f"monthly budget of {self._budget} requests used up")
        self._used[month] = self._used.get(month, 0) + 1
        query = urllib.parse.urlencode({"access_key": self._key, "flight_iata": flight_iata, "limit": 1})
        try:
            status, data = self._transport("GET", f"{BASE}/flights?{query}", {}, None)
        except HttpError as e:
            raise AviationStackError(str(e)) from None
        if status != 200 or not isinstance(data, dict) or "error" in data:
            err = data.get("error", {}) if isinstance(data, dict) else {}
            raise AviationStackError(f"aviationstack http {status} {err.get('code', '')}".strip())
        records = [r for r in (parse_flight(x) for x in data.get("data", [])) if r]
        result = records[0] if records else None
        self._cache[flight_iata] = (self._clock(), result)
        return result
