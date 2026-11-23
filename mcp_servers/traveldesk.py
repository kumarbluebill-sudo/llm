"""Travel Desk B2B public API client (tours/activities; UAE inventory, AED by default).

Read calls (balance, tours, offers, availability) are always allowed. The three MUTATING calls
(reserve / confirm / cancel) move agency money or inventory, so they are refused unless ALL of:
  1. TRAVELDESK_ALLOW_LIVE_BOOKING=true,
  2. the key's issuer is the TEST environment (or APP_ENV=production AND TRAVELDESK_ALLOW_PRODUCTION=1),
  3. the caller constructed the client with allow_booking=True.
A key is only ever sent to the host that matches its own issuer claim."""
import base64
import json
import os
from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from mcp_servers.http import HttpError, Transport, urllib_transport

TEST_BASE = "https://testapi.traveldesk.me/public/b2b/v1"
PROD_BASE = "https://api.traveldesk.me/public/b2b/v1"


class TravelDeskError(Exception):
    pass


class LiveBookingDisabled(TravelDeskError):
    pass


def token_issuer(token: str) -> str | None:
    """Read the (unverified, non-secret) issuer claim of a JWT-style key to learn its environment."""
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("iss")
    except (IndexError, ValueError, TypeError):
        return None


def environment_of(token: str) -> str:
    iss = token_issuer(token) or ""
    return "test" if iss.startswith("test.") else "production" if iss else "unknown"


class Offer(BaseModel):
    id: int
    name: str
    tour_id: int | None = None
    tour_name: str | None = None
    currency: str = "aed"
    per_adult: float | None = None
    per_child: float | None = None
    per_trip: float | None = None


def parse_offer(raw: dict) -> Offer | None:
    if "id" not in raw or not raw.get("name"):
        return None
    prices = raw.get("prices") or []
    p = prices[0] if prices else {}
    return Offer(
        id=int(raw["id"]), name=str(raw["name"]), tour_id=raw.get("tourId"), tour_name=raw.get("tourName"),
        currency=str(p.get("currency", "aed")), per_adult=p.get("recPricePerAdult"),
        per_child=p.get("recPricePerChild"), per_trip=p.get("recPricePerTrip"),
    )  # fmt: skip


class ReserveRequest(BaseModel):
    """BookingCreateB2bCommandRequest. referenceNumber is OUR idempotency/reference key."""

    model_config = {"populate_by_name": True}
    offer_id: int = Field(alias="offerId")
    date: date
    adults: int = Field(ge=1, le=50)
    children: int = Field(default=0, ge=0, le=50)
    infants: int = Field(default=0, ge=0, le=50)
    guest_name: str = Field(alias="guestName", min_length=2, max_length=120)
    reference_number: str = Field(alias="referenceNumber", min_length=3, max_length=64)
    total_cost: float = Field(alias="totalCost", gt=0)
    total_cost_usd: float = Field(alias="totalCostUsd", gt=0)
    currency: str | None = None
    email: str | None = None
    phone: str | None = None
    remarks: str | None = None
    pay_by_balance: float | None = Field(default=None, alias="payByBalance", ge=0)

    def payload(self) -> dict:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


class TravelDeskClient:
    def __init__(self, api_key: str, base: str | None = None, transport: Transport = urllib_transport, allow_booking: bool = False):
        self._key, self._transport, self._allow_booking = api_key, transport, allow_booking
        self.environment = environment_of(api_key)
        default = TEST_BASE if self.environment == "test" else PROD_BASE
        self.base = (base or default).rstrip("/")
        is_test_host = "testapi." in self.base
        if (self.environment == "test") != is_test_host and self.environment != "unknown":
            raise TravelDeskError("refusing to send a key to a host that does not match its environment")

    @classmethod
    def from_env(cls, **kw) -> "TravelDeskClient | None":
        key = os.getenv("TRAVELDESK_API_KEY")
        if not key:
            return None
        return cls(key, allow_booking=os.getenv("TRAVELDESK_ALLOW_LIVE_BOOKING", "").lower() == "true", **kw)

    # --- plumbing ------------------------------------------------------------
    def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> Any:
        url = self.base + path
        if params:
            from urllib.parse import urlencode

            url += "?" + urlencode({k: ("true" if v is True else "false" if v is False else v) for k, v in params.items() if v is not None})
        headers = {"Authorization": f"Bearer {self._key}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            status, data = self._transport(method, url, headers, body)
        except HttpError as e:
            raise TravelDeskError(str(e).replace(self._key, "***")) from None
        if status not in (200, 201, 204):
            detail = str(data)[:120] if data else ""
            raise TravelDeskError(f"traveldesk {method} {path} failed: http {status} {detail}".replace(self._key, "***").strip())
        return data

    def _require_booking(self) -> None:
        prod_ok = os.getenv("APP_ENV") == "production" and os.getenv("TRAVELDESK_ALLOW_PRODUCTION") == "1"
        if not self._allow_booking:
            raise LiveBookingDisabled("booking calls are disabled (set TRAVELDESK_ALLOW_LIVE_BOOKING=true and opt in explicitly)")
        if self.environment != "test" and not prod_ok:
            raise LiveBookingDisabled("production bookings need APP_ENV=production and TRAVELDESK_ALLOW_PRODUCTION=1")

    # --- reads -----------------------------------------------------------------
    def balance(self) -> dict:
        return self._request("GET", "/balance")

    def categories(self, language: str | None = None) -> list[dict]:
        return self._request("GET", "/tourscategories", {"Language": language})

    def tours(self, currency: str | None = None, country_id: int | None = None) -> list[dict]:
        return self._request("GET", "/tours", {"CurrencyCode": currency, "CountryId": country_id})

    def offers(self, include_prices: bool = True, country_id: int | None = None) -> list[Offer]:
        raw = self._request("GET", "/offers", {"IncludePrices": include_prices, "CountryId": country_id})
        return [o for r in raw or [] if (o := parse_offer(r))]

    def availability(self, offer_id: int, start: date, end: date, currency: str | None = None) -> Any:
        return self._request("GET", "/availability/byoffer", {"Id": offer_id, "From": start.isoformat(), "To": end.isoformat(), "CurrencyCode": currency})

    def get_booking(self, booking_id: int) -> dict:
        return self._request("GET", f"/bookings/{int(booking_id)}")

    # --- mutating (gated) ----------------------------------------------------------
    def reserve(self, req: ReserveRequest) -> dict:
        """Create a reservation that is held for a limited time (our HOLD step)."""
        self._require_booking()
        return self._request("POST", "/bookings/reserve", body=req.payload())

    def confirm(self, booking_id: int) -> dict:
        self._require_booking()
        return self._request("POST", "/bookings/confirm", body={"id": int(booking_id)})

    def cancel(self, booking_id: int) -> dict:
        self._require_booking()
        return self._request("POST", "/bookings/cancel", body={"id": int(booking_id)})
