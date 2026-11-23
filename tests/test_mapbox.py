import os

import pytest

from agents.aggregator import aggregate
from agents.pricer import price
from api.config import load_env
from api.schemas import Manifest
from mcp_servers.http import HttpError
from mcp_servers.mapbox import (
    AIRPORT_COORDS,
    STADIUMS,
    HybridRoutes,
    MapboxClient,
    MapboxError,
    MapboxRoutes,
    estimate_fare_inr,
    haversine_km,
)
from mcp_servers.mock_suppliers import MockTransport
from mcp_servers.resilience import Guarded
from mcp_servers.toolbox import build_toolbox, mock_toolbox

TOKEN = "pk.test-token-should-never-leak"
CENTRE = (76.3234, 32.2190)  # Dharamshala
STADIUM_OK = (76.3256, 32.1976)  # ~2.4 km from centre
MOHALI = (76.7334, 30.6932)  # what a naive geocode actually returned (wrong city)


class FakeMapbox:
    def __init__(self, stadium=STADIUM_OK, centre=CENTRE, minutes=22, km=14.2, status=200):
        self.calls, self.stadium, self.centre, self.minutes, self.km, self.status = [], stadium, centre, minutes, km, status

    def __call__(self, method, url, headers, body):
        self.calls.append(url)
        if self.status != 200:
            return self.status, {"message": f"Not Authorized - Invalid Token {TOKEN}"}
        if "/directions/" in url:
            return 200, {"routes": [{"duration": self.minutes * 60, "distance": self.km * 1000}]}
        if "types=place" in url:
            return 200, {"features": [{"center": list(self.centre), "place_name": "Dharamshala, India"}] if self.centre else []}
        return 200, {"features": [{"center": list(self.stadium), "place_name": "Some Stadium"}] if self.stadium else []}


def routes(**kw):
    fake = FakeMapbox(**kw)
    return MapboxRoutes(MapboxClient(TOKEN, fake)), fake


def test_haversine_and_fare_estimate():
    assert haversine_km(CENTRE, CENTRE) == 0
    assert 1100 < haversine_km((77.1, 28.55), (72.87, 19.09)) < 1200  # Delhi-Mumbai ~1140 km great-circle
    assert haversine_km(CENTRE, MOHALI) > 150
    assert estimate_fare_inr(1) == 300 and estimate_fare_inr(14.2) == 430 and estimate_fare_inr(100) == 2150


def test_every_priced_airport_and_stadium_is_covered():
    from agents.knowledge import AIRPORTS

    assert set(AIRPORTS.values()) <= set(AIRPORT_COORDS) and set(AIRPORTS) <= set(STADIUMS)


def test_trusted_stadium_is_used():
    r, fake = routes()
    route = r.get_route("DGH", "Dharamshala")
    assert (route.minutes, route.source) == (22, "mapbox") and route.cost_inr == estimate_fare_inr(14.2)
    assert route.note == "Driving time to HPCA Cricket Stadium"
    stadium_url = next(u for u in fake.calls if "mapbox.places" in u and "types=place" not in u)
    assert "proximity=76.3234%2C32.219" in stadium_url and "country=in" in stadium_url


def test_wrong_city_geocode_is_rejected_not_trusted():
    r, fake = routes(stadium=MOHALI, minutes=330, km=228)  # the bug we actually hit
    route = r.get_route("DGH", "Dharamshala")
    directions = next(u for u in fake.calls if "/directions/" in u)
    assert "76.3234,32.219" in directions.replace("%2C", ",")  # routed to the city centre instead
    assert "city centre" in route.note and "could not be located reliably" in route.note


def test_city_centre_far_from_its_airport_is_rejected():
    far = (79.0, 30.0)  # a same-name place hundreds of km away (the "Delhi" bug)
    r, _ = routes(centre=far)
    with pytest.raises(MapboxError, match="could not place"):
        r.get_route("DGH", "Dharamshala")


def test_delhi_tries_new_delhi_first():
    r, fake = routes(centre=(77.209, 28.614), stadium=(77.243, 28.638), minutes=40, km=17)
    assert r.get_route("DEL", "Delhi").minutes == 40
    first = next(u for u in fake.calls if "types=place" in u)
    assert "New%20Delhi" in first


def test_missing_data_raises_clean_errors():
    r, _ = routes(centre=None)
    with pytest.raises(MapboxError):
        r.get_route("DGH", "Dharamshala")
    with pytest.raises(MapboxError):
        routes()[0].get_route("XXX", "Dharamshala")
    no_stadium, _ = routes(stadium=None)
    assert "city centre" in no_stadium.get_route("DGH", "Dharamshala").note


def test_errors_never_contain_the_token():
    r, _ = routes(status=401)
    with pytest.raises(MapboxError) as e:
        r.get_route("DGH", "Dharamshala")
    assert TOKEN not in str(e.value) and "401" in str(e.value)

    def down(*a):
        raise HttpError(f"network error {TOKEN}")

    with pytest.raises(MapboxError) as e2:
        MapboxClient(TOKEN, down).drive((0, 0), (1, 1))
    assert TOKEN not in str(e2.value)


def test_hybrid_falls_back_to_labelled_sample_and_opens_breaker():
    fake = FakeMapbox(status=500)
    hy = HybridRoutes(MapboxRoutes(MapboxClient(TOKEN, fake)), MockTransport())
    for _ in range(8):
        route = hy.get_route("DGH", "Dharamshala")
    assert route.source == "mock"
    assert len(fake.calls) == 3  # first call per attempt fails; breaker opens after three failures


def manifest():
    return Manifest.model_validate({
        "trip_id": "t", "event": {"name": "RCB vs GT", "venue": "Dharamshala", "date": "2027-04-15"},
        "travelers": {"adults": 2}, "budget": {"tier": "mid", "max_total": 150000},
    })  # fmt: skip


def test_itinerary_uses_real_route_and_says_the_fare_is_an_estimate():
    tb = mock_toolbox()
    tb._transport = Guarded(HybridRoutes(MapboxRoutes(MapboxClient(TOKEN, FakeMapbox())), MockTransport()), retries=0)
    it = aggregate(manifest(), price(manifest(), tb))
    transfers = [s for s in it.segments if s.type == "transfer"]
    assert all(s.details["source"] == "mapbox" and s.details["minutes"] == 22 for s in transfers)
    assert any("Mapbox driving directions" in w and "estimate" in w for w in it.warnings)


def test_build_toolbox_uses_mapbox_only_when_configured(monkeypatch):
    monkeypatch.delenv("MAPBOX_API_KEY", raising=False)
    assert type(build_toolbox()._transport.targets[0][1]).__name__ == "MockTransport"
    monkeypatch.setenv("MAPBOX_API_KEY", TOKEN)
    assert type(build_toolbox()._transport.targets[0][1]).__name__ == "HybridRoutes"
    monkeypatch.setenv("MAPBOX_LIVE", "0")
    assert type(build_toolbox()._transport.targets[0][1]).__name__ == "MockTransport"


live = pytest.mark.skipif(os.getenv("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1")


@live
@pytest.mark.parametrize("airport,city", [("DGH", "Dharamshala"), ("BOM", "Mumbai"), ("DEL", "Delhi")])
def test_live_mapbox_routes_are_plausible(airport, city):
    load_env(force=True)
    client = MapboxClient.from_env()
    assert client is not None
    route = MapboxRoutes(client).get_route(airport, city)
    assert 5 <= route.minutes <= 150, (city, route.minutes, route.note)  # the Mohali bug gave 330
    assert route.source == "mapbox"
