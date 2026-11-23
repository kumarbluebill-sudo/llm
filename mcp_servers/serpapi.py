"""SerpApi (Google Flights + Google Hotels) as a LIVE PRICE SOURCE. Search only:

- no booking, and no fare rules / cancellation terms, so refundability is reported as UNKNOWN
  (never assumed) and itineraries built from this data are not bookable;
- the free plan allows 250 searches a month, so the client reads the real balance from the
  (free) account endpoint, keeps a reserve, and everything is cached by the callers;
- the API key is a query parameter: HTTPS only, and it is scrubbed from every error message.
When the live source fails or the budget is spent we fall back to the mock suppliers and the
offers say source="mock", so the UI can tell sample data from live data."""
import os
import re
import urllib.parse
from datetime import date
from typing import Any

from mcp_servers.http import HttpError, Transport, urllib_transport
from mcp_servers.mock_suppliers import MockFlights, MockHotels
from mcp_servers.models import FareRules, FlightOffer, HotelOffer
from mcp_servers.resilience import CircuitBreaker

BASE = "https://serpapi.com"
DEFAULT_RESERVE = 30


class SerpApiError(Exception):
    pass


class SerpApiQuotaExceeded(SerpApiError):
    pass


class SerpApiClient:
    def __init__(self, api_key: str, transport: Transport = urllib_transport, reserve: int = DEFAULT_RESERVE):
        self._key, self._transport, self._reserve = api_key, transport, reserve
        self._left: int | None = None

    @classmethod
    def from_env(cls, **kw) -> "SerpApiClient | None":
        key = os.getenv("SERPAPI_API_KEY")
        return cls(key, **kw) if key else None

    def _scrub(self, text: str) -> str:
        return text.replace(self._key, "***")

    def _get(self, path: str, params: dict[str, str]) -> Any:
        query = urllib.parse.urlencode({**params, "api_key": self._key})
        try:
            status, data = self._transport("GET", f"{BASE}/{path}?{query}", {}, None)
        except HttpError as e:
            raise SerpApiError(self._scrub(str(e))) from None
        if status != 200 or not isinstance(data, dict) or data.get("error"):
            msg = str(data.get("error", ""))[:80] if isinstance(data, dict) else ""
            raise SerpApiError(self._scrub(f"serpapi http {status} {msg}".strip()))
        return data

    @property
    def searches_left(self) -> int:
        if self._left is None:  # account.json is free and does not use a search
            data = self._get("account.json", {})
            self._left = int(data.get("total_searches_left", data.get("plan_searches_left", 0)))
        return self._left

    def search(self, **params: str) -> dict:
        if self.searches_left <= self._reserve:
            raise SerpApiQuotaExceeded(f"only {self.searches_left} searches left (reserve {self._reserve})")
        data = self._get("search.json", params)
        self._left = self.searches_left - 1  # conservative: cached hits are free but we still count
        return data


# --- parsing -----------------------------------------------------------------
def _hhmm(stamp: str) -> str:
    return stamp.split(" ")[1] if " " in stamp else stamp[-5:]


def parse_flights(data: dict, origin: str, dest: str, day: date, limit: int = 8) -> list[FlightOffer]:
    items = (data.get("best_flights") or []) + (data.get("other_flights") or [])
    rows: list[dict] = []
    seen: set[tuple] = set()
    for it in items:
        legs, price = it.get("flights") or [], it.get("price")
        if not legs or price is None:
            continue
        first = legs[0]
        depart = _hhmm(first["departure_airport"]["time"])
        key = (first.get("flight_number"), depart)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "price": int(price), "depart": depart, "layovers": len(it.get("layovers") or []),
            "duration": int(it.get("total_duration") or sum(int(x.get("duration", 0)) for x in legs)),
            "carrier": first.get("airline") or "Unknown", "number": first.get("flight_number"),
        })  # fmt: skip
    rows.sort(key=lambda r: (r["price"], r["depart"]))
    return [
        FlightOffer(
            offer_id=f"{origin}-{dest}-{day.isoformat()}-s{i}",  # "s" = live; mock ids are plain numbers
            origin=origin, dest=dest, day=day, price=r["price"],
            refundable_listed=False, refundable_known=False, layovers=r["layovers"],
            depart=r["depart"], duration_min=r["duration"], carrier=r["carrier"],
            flight_number=r["number"], source="serpapi",
        )
        for i, r in enumerate(rows[:limit])
    ]  # fmt: skip


def parse_hotels(data: dict, limit: int = 8) -> list[HotelOffer]:
    out: list[HotelOffer] = []
    for p in data.get("properties") or []:
        rate = (p.get("rate_per_night") or {}).get("extracted_lowest")
        token, name = p.get("property_token"), p.get("name")
        if rate is None or not token or not name or p.get("type", "hotel") != "hotel":
            continue
        m = re.search(r"(\d)-star", str(p.get("hotel_class") or ""))
        out.append(
            HotelOffer(
                hotel_id=f"serp-{token[:24]}", name=name, stars=int(m.group(1)) if m else 0,
                nightly_price=int(rate), refundable=False, refundable_known=False, source="serpapi",
            )
        )  # fmt: skip
    out.sort(key=lambda h: h.nightly_price)
    return out[:limit]


# --- providers (same interface as the mocks) ---------------------------------------------
class SerpApiFlights:
    def __init__(self, client: SerpApiClient):
        self._client = client
        self.known_ids: set[str] = set()

    def search_flights(self, origin: str, dest: str, day: date, refundable_only: bool = True) -> list[FlightOffer]:
        data = self._client.search(
            engine="google_flights", departure_id=origin, arrival_id=dest,
            outbound_date=day.isoformat(), type="2", currency="INR", hl="en", gl="in",
        )  # fmt: skip
        offers = parse_flights(data, origin, dest, day)
        self.known_ids.update(o.offer_id for o in offers)
        return offers

    def get_fare_rules(self, offer_id: str) -> FareRules:
        # Google Flights exposes no fare rules: say so instead of guessing.
        return FareRules(offer_id=offer_id, refundable=False, cancellation_fee=0, known=False)


class SerpApiHotels:
    def __init__(self, client: SerpApiClient):
        self._client = client

    def search_hotels(self, venue: str, refundable_only: bool = True, checkin: date | None = None,
                      checkout: date | None = None) -> list[HotelOffer]:  # fmt: skip
        if checkin is None or checkout is None:
            raise SerpApiError("live hotel search needs check-in and check-out dates")
        data = self._client.search(
            engine="google_hotels", q=f"{venue} hotels", check_in_date=checkin.isoformat(),
            check_out_date=checkout.isoformat(), adults="2", currency="INR", gl="in", hl="en",
        )  # fmt: skip
        return parse_hotels(data)


class HybridFlights:
    """Live first; sample data (source="mock") when live is down, over budget or returns nothing."""

    def __init__(self, live: SerpApiFlights, mock: MockFlights | None = None):
        self.live, self.mock = live, mock or MockFlights()
        self.breaker = CircuitBreaker(failure_threshold=3, reset_after=300)

    def search_flights(self, origin, dest, day, refundable_only=True):
        if self.breaker.allow():
            try:
                offers = self.live.search_flights(origin, dest, day, refundable_only)
            except SerpApiError:
                self.breaker.record_failure()
            else:
                self.breaker.record_success()
                if offers:
                    return offers
        return self.mock.search_flights(origin, dest, day, refundable_only)

    def get_fare_rules(self, offer_id):
        if offer_id.rsplit("-", 1)[1].startswith("s"):  # live ids carry an "s" prefix (survives restarts)
            return self.live.get_fare_rules(offer_id)
        return self.mock.get_fare_rules(offer_id)


class HybridHotels:
    def __init__(self, live: SerpApiHotels, mock: MockHotels | None = None):
        self.live, self.mock = live, mock or MockHotels()
        self.breaker = CircuitBreaker(failure_threshold=3, reset_after=300)

    def search_hotels(self, venue, refundable_only=True, checkin=None, checkout=None):
        if self.breaker.allow():
            try:
                offers = self.live.search_hotels(venue, refundable_only, checkin, checkout)
            except SerpApiError:
                self.breaker.record_failure()
            else:
                self.breaker.record_success()
                if offers:
                    return offers
        return self.mock.search_hotels(venue, refundable_only)
