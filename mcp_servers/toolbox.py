"""Tool layer used by agents. Each supplier is wrapped in breaker/retry/cache. The method
names mirror the MCP tools in the spec; an MCP transport wrapper can be added on top."""
import os
from datetime import date

from mcp_servers.mock_suppliers import (
    MockActivities,
    MockFlights,
    MockHotels,
    MockTransport,
    MockWeather,
)
from mcp_servers.models import Activity, FareRules, FlightOffer, Forecast, HotelOffer, Route
from mcp_servers.resilience import Guarded
from mcp_servers.serpapi import (
    HybridFlights,
    HybridHotels,
    SerpApiClient,
    SerpApiFlights,
    SerpApiHotels,
)
from mcp_servers.viator import ViatorActivities, pick_credentials


class Toolbox:
    def __init__(
        self, flights: Guarded, hotels: Guarded, transport: Guarded, weather: Guarded,
        activities: Guarded | None = None,
    ):
        self._flights, self._hotels = flights, hotels
        self._transport, self._weather = transport, weather
        self._activities = activities

    def search_flights(
        self, origin: str, dest: str, day: date, refundable_only: bool = True
    ) -> list[FlightOffer]:
        return self._flights.call("search_flights", origin, dest, day, refundable_only, cacheable=True)

    def get_fare_rules(self, offer_id: str) -> FareRules:
        return self._flights.call("get_fare_rules", offer_id, cacheable=True)

    def search_hotels(
        self, venue: str, refundable_only: bool = True, checkin: date | None = None, checkout: date | None = None
    ) -> list[HotelOffer]:
        return self._hotels.call("search_hotels", venue, refundable_only, checkin, checkout, cacheable=True)

    def get_route(self, airport: str, venue: str) -> Route:
        return self._transport.call("get_route", airport, venue, cacheable=True)

    def forecast(self, venue: str, day: date) -> Forecast:
        return self._weather.call("forecast", venue, day, cacheable=True)

    def search_activities(self, venue: str, limit: int = 6) -> list[Activity]:
        if self._activities is None:
            return []
        return self._activities.call("search_activities", venue, limit, cacheable=True)


def mock_toolbox() -> Toolbox:
    return Toolbox(
        Guarded(MockFlights()),
        Guarded(MockHotels()),
        Guarded(MockTransport()),
        Guarded(MockWeather()),
        Guarded(MockActivities()),
    )


def build_toolbox() -> Toolbox:
    """Mock flights/hotels/transport/weather (no live supplier keys yet). Activities come from
    Viator when a key is configured (VIATOR_LIVE=0 turns that off), otherwise from the mock."""
    tb = mock_toolbox()
    creds = pick_credentials()
    if creds and os.getenv("VIATOR_LIVE", "1") != "0":
        key, base = creds
        tb._activities = Guarded(ViatorActivities(key, base), cache_ttl=900, retries=1)
    serp = SerpApiClient.from_env() if os.getenv("SERPAPI_LIVE", "1") != "0" else None
    if serp is not None:  # live Google prices (search only); 1 hour cache keeps the 250/month quota alive
        tb._flights = Guarded(HybridFlights(SerpApiFlights(serp)), cache_ttl=3600, retries=0)
        tb._hotels = Guarded(HybridHotels(SerpApiHotels(serp)), cache_ttl=3600, retries=0)
    return tb
