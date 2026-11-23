"""Tool layer used by agents. Each supplier is wrapped in breaker/retry/cache. The method
names mirror the MCP tools in the spec; an MCP transport wrapper can be added on top."""
from datetime import date

from mcp_servers.mock_suppliers import MockFlights, MockHotels, MockTransport, MockWeather
from mcp_servers.models import FareRules, FlightOffer, Forecast, HotelOffer, Route
from mcp_servers.resilience import Guarded


class Toolbox:
    def __init__(self, flights: Guarded, hotels: Guarded, transport: Guarded, weather: Guarded):
        self._flights, self._hotels = flights, hotels
        self._transport, self._weather = transport, weather

    def search_flights(
        self, origin: str, dest: str, day: date, refundable_only: bool = True
    ) -> list[FlightOffer]:
        return self._flights.call("search_flights", origin, dest, day, refundable_only, cacheable=True)

    def get_fare_rules(self, offer_id: str) -> FareRules:
        return self._flights.call("get_fare_rules", offer_id, cacheable=True)

    def search_hotels(self, venue: str, refundable_only: bool = True) -> list[HotelOffer]:
        return self._hotels.call("search_hotels", venue, refundable_only, cacheable=True)

    def get_route(self, airport: str, venue: str) -> Route:
        return self._transport.call("get_route", airport, venue, cacheable=True)

    def forecast(self, venue: str, day: date) -> Forecast:
        return self._weather.call("forecast", venue, day, cacheable=True)


def mock_toolbox() -> Toolbox:
    return Toolbox(
        Guarded(MockFlights()),
        Guarded(MockHotels()),
        Guarded(MockTransport()),
        Guarded(MockWeather()),
    )
