"""Deterministic mock suppliers (no network). `fail_times` injects transient failures."""
import zlib
from datetime import date

from mcp_servers.models import Activity, FareRules, FlightOffer, Forecast, HotelOffer, Route

# Listing flag vs. authoritative fare rule. Index 1 is a trap: listed refundable, actually not.
_LISTED_REFUNDABLE = [False, True, True, True]
_REAL_REFUNDABLE = [False, False, True, True]
_FLIGHT_FACTORS = [0.8, 0.9, 1.0, 1.4]
_HOTEL_FACTORS = [1.0, 1.5, 2.2, 3.5]
_HOTEL_REFUNDABLE = [False, True, True, True]
_DEPART = ["06:15", "09:40", "13:05", "18:30"]
_CARRIERS = ["SpiceJet", "IndiGo", "Air India", "Akasa Air"]


def _seed(*parts: object) -> int:
    return zlib.crc32("|".join(map(str, parts)).encode())


class _Flaky:
    def __init__(self, fail_times: int = 0):
        self.fail_times = fail_times
        self.calls = 0

    def _maybe_fail(self) -> None:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError("injected supplier failure")


class MockFlights(_Flaky):
    def search_flights(
        self, origin: str, dest: str, day: date, refundable_only: bool = True
    ) -> list[FlightOffer]:
        self._maybe_fail()
        base = 3500 + _seed(origin, dest) % 4000
        offers = []
        for i, factor in enumerate(_FLIGHT_FACTORS):
            if refundable_only and not _LISTED_REFUNDABLE[i]:
                continue
            offers.append(
                FlightOffer(
                    offer_id=f"{origin}-{dest}-{day.isoformat()}-{i}",
                    origin=origin,
                    dest=dest,
                    day=day,
                    price=round(base * factor),
                    refundable_listed=_LISTED_REFUNDABLE[i],
                    layovers=1 if i == 0 else 0,
                    depart=_DEPART[i],
                    duration_min=70 + _seed(origin, dest, i) % 80,
                    carrier=_CARRIERS[i],
                )
            )
        return offers

    def get_fare_rules(self, offer_id: str) -> FareRules:
        self._maybe_fail()
        i = int(offer_id.rsplit("-", 1)[1])
        refundable = _REAL_REFUNDABLE[i]
        return FareRules(offer_id=offer_id, refundable=refundable, cancellation_fee=0 if refundable else 3000)


class MockHotels(_Flaky):
    def search_hotels(self, venue: str, refundable_only: bool = True) -> list[HotelOffer]:
        self._maybe_fail()
        base = 1800 + _seed(venue) % 1500
        offers = []
        for i, factor in enumerate(_HOTEL_FACTORS):
            if refundable_only and not _HOTEL_REFUNDABLE[i]:
                continue
            offers.append(
                HotelOffer(
                    hotel_id=f"{venue.lower()}-h{i}",
                    name=f"{venue} Stay {i + 1}",
                    stars=i + 2,
                    nightly_price=round(base * factor),
                    refundable=_HOTEL_REFUNDABLE[i],
                )
            )
        return offers


class MockTransport(_Flaky):
    def get_route(self, airport: str, venue: str) -> Route:
        self._maybe_fail()
        s = _seed(airport, venue)
        return Route(airport=airport, venue=venue, minutes=25 + s % 90, cost_inr=600 + s % 1800)


class MockWeather(_Flaky):
    def forecast(self, venue: str, day: date) -> Forecast:
        self._maybe_fail()
        rain = (_seed(venue, day.isoformat()) % 100) / 100
        summary = "Rain likely" if rain >= 0.6 else "Mostly clear"
        return Forecast(venue=venue, day=day, summary=summary, rain_probability=rain)


class MockActivities(_Flaky):
    def search_activities(self, venue: str, limit: int = 6) -> list[Activity]:
        self._maybe_fail()
        kinds = ["Heritage walk", "Local food tour", "Sunrise viewpoint trip", "Museum and old town"]
        out = []
        for i, kind in enumerate(kinds[:limit]):
            s = _seed(venue, i)
            out.append(
                Activity(
                    product_code=f"MOCK-{venue[:3].upper()}-{i}",
                    title=f"{kind} in {venue}",
                    from_price=800 + s % 3000,
                    duration_min=90 + (s % 4) * 45,
                    rating=round(4.0 + (s % 10) / 10, 1),
                    reviews=20 + s % 400,
                    source="mock",
                )
            )
        return out
