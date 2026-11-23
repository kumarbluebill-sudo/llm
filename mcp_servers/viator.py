"""Viator Partner API (v2) client: destination lookup + activity search (read-only).

Sandbox by default. Production is used only when VIATOR_API_ENV=production AND APP_ENV=production,
because production use carries Viator's certification/caching/attribution obligations."""
import os
from typing import Any

from mcp_servers.http import HttpError, Transport, urllib_transport
from mcp_servers.models import Activity

SANDBOX = "https://api.sandbox.viator.com/partner"
PRODUCTION = "https://api.viator.com/partner"

# Our city names -> names Viator uses (first match wins).
ALIASES: dict[str, list[str]] = {
    "Dharamshala": ["Dharamsala", "Dharamshala"],
    "Delhi": ["New Delhi", "Delhi"],
    "Bengaluru": ["Bengaluru", "Bangalore"],
    "Kolkata": ["Kolkata", "Calcutta"],
    "Chennai": ["Chennai", "Madras"],
}


class ViatorError(Exception):
    pass


def pick_credentials(env: dict[str, str] | None = None) -> tuple[str, str] | None:
    """Return (api_key, base_url) or None. Production needs two explicit opt-ins."""
    e = env if env is not None else os.environ
    prod = e.get("VIATOR_API_ENV") == "production" and e.get("APP_ENV") == "production"
    key = e.get("VIATOR_PRODUCTION_KEY") if prod else e.get("VIATOR_SANDBOX_KEY")
    if not key:
        return None
    return key, PRODUCTION if prod else SANDBOX


def _duration(p: dict) -> int | None:
    d = p.get("duration") or {}
    return d.get("fixedDurationInMinutes") or d.get("variableDurationFromMinutes")


def _image(p: dict) -> str | None:
    for img in p.get("images") or []:
        variants = sorted(img.get("variants") or [], key=lambda v: abs((v.get("width") or 0) - 480))
        if variants and str(variants[0].get("url", "")).startswith("https://"):
            return variants[0]["url"]
    return None


def parse_product(p: dict) -> Activity | None:
    price = ((p.get("pricing") or {}).get("summary") or {}).get("fromPrice")
    if not p.get("productCode") or not p.get("title") or price is None:
        return None
    reviews = p.get("reviews") or {}
    url = p.get("productUrl")
    return Activity(
        product_code=str(p["productCode"]),
        title=str(p["title"]),
        from_price=round(float(price)),
        currency=str((p.get("pricing") or {}).get("currency", "INR")),
        duration_min=_duration(p),
        rating=reviews.get("combinedAverageRating"),
        reviews=reviews.get("totalReviews"),
        url=url if isinstance(url, str) and url.startswith("https://") else None,
        image=_image(p),
        source="viator",
    )


class ViatorActivities:
    def __init__(self, api_key: str, base: str = SANDBOX, transport: Transport = urllib_transport, currency: str = "INR"):
        self._base, self._transport, self._currency = base, transport, currency
        self._headers = {
            "exp-api-key": api_key,
            "Accept": "application/json;version=2.0",
            "Accept-Language": "en-US",
            "Content-Type": "application/json",
        }
        self._destinations: dict[str, int] | None = None

    def _call(self, method: str, path: str, body: dict | None = None) -> Any:
        try:
            status, data = self._transport(method, self._base + path, self._headers, body)
        except HttpError as e:
            raise ViatorError(str(e)) from None
        if status != 200:
            code = data.get("code") if isinstance(data, dict) else None
            raise ViatorError(f"viator {path} failed: http {status} {code or ''}".strip())
        return data

    def _load_destinations(self) -> dict[str, int]:
        if self._destinations is None:
            data = self._call("GET", "/destinations")
            names: dict[str, int] = {}
            for d in data.get("destinations", []):
                # Prefer cities: first CITY entry wins, otherwise first entry of that name.
                name = d.get("name")
                if name and (name not in names or d.get("type") == "CITY"):
                    names[name] = int(d["destinationId"])
            self._destinations = names
        return self._destinations

    def destination_id(self, venue: str) -> int | None:
        names = self._load_destinations()
        return next((names[n] for n in ALIASES.get(venue, [venue]) if n in names), None)

    def search_activities(self, venue: str, limit: int = 6) -> list[Activity]:
        did = self.destination_id(venue)
        if did is None:
            return []
        body = {
            "filtering": {"destination": str(did)},
            "currency": self._currency,
            "pagination": {"start": 1, "count": limit},
        }
        data = self._call("POST", "/products/search", body)
        return [a for p in data.get("products", []) if (a := parse_product(p))][:limit]
