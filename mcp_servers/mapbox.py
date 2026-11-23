"""Mapbox driving directions for airport -> venue transfers.

Geocoding a stadium by name can silently return the WRONG place (a naive "HPCA Stadium
Dharamshala" lookup returned a stadium near Mohali, 330 minutes away). So the venue point is
only trusted when it lies within MAX_VENUE_KM of the city centre; otherwise we route to the city
centre and say so. The fare is a per-km ESTIMATE; only the time and distance come from Mapbox."""
import math
import os
import urllib.parse
from typing import Any

from mcp_servers.http import HttpError, Transport, urllib_transport
from mcp_servers.mock_suppliers import MockTransport
from mcp_servers.models import Route
from mcp_servers.resilience import CircuitBreaker

BASE = "https://api.mapbox.com"
MAX_VENUE_KM = 25.0  # a venue must be this close to the city centre
MAX_CITY_KM = 80.0  # ...and the city centre this close to its airport (catches same-name places elsewhere)

# Queries to try, in order, when geocoding a city centre ("Delhi" alone matched a place hundreds of km away).
CITY_QUERIES: dict[str, list[str]] = {"Delhi": ["New Delhi, India", "Delhi, India"]}

# (lon, lat) of the airports we price. Approximate to ~1 km, which is plenty for drive time.
AIRPORT_COORDS: dict[str, tuple[float, float]] = {
    "DGH": (76.263, 32.165), "DEL": (77.100, 28.556), "BOM": (72.866, 19.089), "BLR": (77.707, 13.199),
    "MAA": (80.171, 12.994), "CCU": (88.447, 22.655), "AMD": (72.635, 23.077), "HYD": (78.429, 17.240),
    "JAI": (75.812, 26.824), "LKO": (80.889, 26.761),
}  # fmt: skip

STADIUMS = {
    "Dharamshala": "HPCA Cricket Stadium", "Mumbai": "Wankhede Stadium", "Bengaluru": "M. Chinnaswamy Stadium",
    "Chennai": "MA Chidambaram Stadium", "Kolkata": "Eden Gardens", "Delhi": "Arun Jaitley Stadium",
    "Ahmedabad": "Narendra Modi Stadium", "Hyderabad": "Rajiv Gandhi International Cricket Stadium",
    "Jaipur": "Sawai Mansingh Stadium", "Lucknow": "Ekana Cricket Stadium",
}  # fmt: skip


class MapboxError(Exception):
    pass


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    (lon1, lat1), (lon2, lat2) = a, b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def estimate_fare_inr(km: float) -> int:
    """Rough taxi fare: base + per-km, rounded to 10. An estimate, not a quote."""
    return max(300, round((150 + 20 * km) / 10) * 10)


class MapboxClient:
    def __init__(self, token: str, transport: Transport = urllib_transport):
        self._token, self._transport = token, transport

    @classmethod
    def from_env(cls, **kw) -> "MapboxClient | None":
        tok = os.getenv("MAPBOX_API_KEY")
        return cls(tok, **kw) if tok else None

    def _get(self, path: str, **params: str) -> Any:
        url = f"{BASE}/{urllib.parse.quote(path, safe='/:,;.')}?" + urllib.parse.urlencode({**params, "access_token": self._token})
        try:
            status, data = self._transport("GET", url, {}, None)
        except HttpError as e:
            raise MapboxError(str(e).replace(self._token, "***")) from None
        if status != 200 or not isinstance(data, dict):
            msg = str(data.get("message", ""))[:80] if isinstance(data, dict) else ""
            raise MapboxError(f"mapbox http {status} {msg}".replace(self._token, "***").strip())
        return data

    def geocode(self, query: str, proximity: tuple[float, float] | None = None, types: str | None = None):
        params = {"limit": "1", "country": "in"}
        if proximity:
            params["proximity"] = f"{proximity[0]},{proximity[1]}"
        if types:
            params["types"] = types
        feats = self._get(f"geocoding/v5/mapbox.places/{query}.json", **params).get("features") or []
        if not feats:
            return None
        lon, lat = feats[0]["center"]
        return (lon, lat), str(feats[0].get("place_name", ""))

    def drive(self, a: tuple[float, float], b: tuple[float, float]) -> tuple[int, float]:
        data = self._get(f"directions/v5/mapbox/driving/{a[0]},{a[1]};{b[0]},{b[1]}", overview="false")
        routes = data.get("routes") or []
        if not routes:
            raise MapboxError("no driving route found")
        return round(routes[0]["duration"] / 60), routes[0]["distance"] / 1000


class MapboxRoutes:
    """Same interface as MockTransport.get_route(airport, venue)."""

    def __init__(self, client: MapboxClient):
        self._client = client

    def get_route(self, airport: str, venue: str) -> Route:
        if airport not in AIRPORT_COORDS:
            raise MapboxError(f"no coordinates for airport {airport}")
        origin = AIRPORT_COORDS[airport]
        centre = None
        for q in CITY_QUERIES.get(venue, [f"{venue}, India"]):
            hit = self._client.geocode(q, types="place")
            if hit and haversine_km(hit[0], origin) <= MAX_CITY_KM:
                centre = hit
                break
        if centre is None:
            raise MapboxError(f"could not place {venue} near airport {airport}")
        target, note = centre[0], f"Driving time to {venue} city centre"
        stadium = STADIUMS.get(venue)
        if stadium:
            hit = self._client.geocode(f"{stadium} {venue}", proximity=centre[0])
            if hit and haversine_km(hit[0], centre[0]) <= MAX_VENUE_KM:
                target, note = hit[0], f"Driving time to {stadium}"
            else:  # the geocoder returned somewhere else: do not trust it
                note = f"Venue could not be located reliably; time is to {venue} city centre"
        minutes, km = self._client.drive(origin, target)
        return Route(airport=airport, venue=venue, minutes=minutes, cost_inr=estimate_fare_inr(km), source="mapbox", note=note)


class HybridRoutes:
    """Mapbox first; labelled sample route when Mapbox is down or cannot place the venue."""

    def __init__(self, live: MapboxRoutes, mock: MockTransport | None = None):
        self.live, self.mock = live, mock or MockTransport()
        self.breaker = CircuitBreaker(failure_threshold=3, reset_after=300)

    def get_route(self, airport: str, venue: str) -> Route:
        if self.breaker.allow():
            try:
                route = self.live.get_route(airport, venue)
            except MapboxError:
                self.breaker.record_failure()
            else:
                self.breaker.record_success()
                return route
        return self.mock.get_route(airport, venue)
