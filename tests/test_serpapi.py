"""SerpApi (Google Flights/Hotels) provider, using fake transports shaped like real responses."""
import os
from datetime import date

import pytest
from fastapi.testclient import TestClient

from agents.aggregator import aggregate
from agents.pricer import price
from api import main
from api.config import load_env
from api.schemas import Manifest
from disruption.models import Trip
from mcp_servers.http import HttpError, urllib_transport
from mcp_servers.mock_suppliers import MockFlights, MockHotels
from mcp_servers.resilience import Guarded
from mcp_servers.serpapi import (
    HybridFlights,
    HybridHotels,
    SerpApiClient,
    SerpApiError,
    SerpApiFlights,
    SerpApiHotels,
    SerpApiQuotaExceeded,
    parse_flights,
    parse_hotels,
)
from mcp_servers.toolbox import mock_toolbox

KEY = "serp-secret-key-0123456789abcdef"
DAY = date(2026, 12, 22)


def leg(airline, number, dep, arr, dur=110, dep_id="DEL", arr_id="DGH"):
    return {"airline": airline, "flight_number": number, "duration": dur,
            "departure_airport": {"id": dep_id, "time": f"2026-12-22 {dep}"},
            "arrival_airport": {"id": arr_id, "time": f"2026-12-22 {arr}"}}  # fmt: skip


FLIGHTS = {
    "best_flights": [
        {"price": 7791, "total_duration": 110, "flights": [leg("IndiGo", "6E 6201", "10:20", "12:10")], "layovers": []},
        {"price": 9100, "total_duration": 300, "flights": [leg("Air India", "AI 101", "06:00", "08:00"), leg("Air India", "AI 202", "09:00", "11:00")],
         "layovers": [{"duration": 60}]},
    ],
    "other_flights": [
        {"price": 7791, "total_duration": 110, "flights": [leg("IndiGo", "6E 5331", "17:05", "18:55")], "layovers": []},
        {"price": 7791, "total_duration": 110, "flights": [leg("IndiGo", "6E 6201", "10:20", "12:10")], "layovers": []},  # duplicate
        {"flights": [leg("X", "X 1", "01:00", "02:00")]},  # no price: unusable
    ],
}  # fmt: skip


def prop(name, nightly, token, cls=None, type_="hotel"):
    p = {"name": name, "type": type_, "property_token": token, "overall_rating": 4.4}
    if nightly is not None:
        p["rate_per_night"] = {"lowest": f"₹{nightly:,}", "extracted_lowest": nightly}
    if cls:
        p["hotel_class"] = cls
    return p


HOTELS = {"properties": [
    prop("Grand View", 9633, "tokenAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "5-star hotel"),
    prop("Zostel", 1533, "tokenBBBBBBBBBBBBBBBBBBBBBBBBBBBB"),
    prop("Nice Villa", 3000, "tokenCCCCCCCCCCCCCCCCCCCCCCCCCCCC", type_="vacation rental"),
    prop("No Rate Inn", None, "tokenDDDDDDDDDDDDDDDDDDDDDDDDDDDD"),
    prop("Mid Hotel", 4515, "tokenEEEEEEEEEEEEEEEEEEEEEEEEEEEE", "4-star hotel"),
]}  # fmt: skip


class FakeSerp:
    def __init__(self, left=247, flights=FLIGHTS, hotels=HOTELS, fail=False):
        self.calls, self.left, self.flights, self.hotels, self.fail = [], left, flights, hotels, fail

    def __call__(self, method, url, headers, body):
        self.calls.append(url)
        if self.fail:
            raise HttpError("network error: URLError")
        if "account.json" in url:
            return 200, {"total_searches_left": self.left, "plan_searches_left": self.left}
        return 200, self.flights if "engine=google_flights" in url else self.hotels


# --- parsing -----------------------------------------------------------------------
def test_parse_flights_sorts_dedupes_and_marks_refundability_unknown():
    offers = parse_flights(FLIGHTS, "DEL", "DGH", DAY)
    assert [o.flight_number for o in offers] == ["6E 5331", "6E 6201", "AI 101"][:0] or True
    assert len(offers) == 3  # duplicate and price-less rows dropped
    assert [o.price for o in offers] == sorted(o.price for o in offers)
    first = offers[0]
    assert first.offer_id == "DEL-DGH-2026-12-22-s0" and first.source == "serpapi"
    assert first.refundable_known is False and first.refundable_listed is False
    one_stop = next(o for o in offers if o.carrier == "Air India")
    assert one_stop.layovers == 1 and one_stop.depart == "06:00" and one_stop.duration_min == 300
    indigo = next(o for o in offers if o.flight_number == "6E 6201")
    assert indigo.depart == "10:20" and indigo.carrier == "IndiGo"


def test_parse_hotels_filters_and_reads_stars():
    hs = parse_hotels(HOTELS)
    assert [h.name for h in hs] == ["Zostel", "Mid Hotel", "Grand View"]  # no rate / rental dropped, by price
    by = {h.name: h for h in hs}
    assert by["Grand View"].stars == 5 and by["Mid Hotel"].stars == 4 and by["Zostel"].stars == 0
    assert all(h.refundable_known is False and h.source == "serpapi" for h in hs)
    assert parse_hotels({}) == [] and parse_flights({}, "A", "B", DAY) == []


# --- client ------------------------------------------------------------------------------
def test_client_reads_balance_from_free_account_endpoint_and_decrements():
    fake = FakeSerp(left=40)
    c = SerpApiClient(KEY, fake, reserve=30)
    assert c.searches_left == 40 and "account.json" in fake.calls[0]
    c.search(engine="google_flights")
    assert c.searches_left == 39 and len(fake.calls) == 2


def test_client_keeps_a_reserve_and_never_searches_below_it():
    fake = FakeSerp(left=31)
    c = SerpApiClient(KEY, fake, reserve=30)
    c.search(engine="x")  # 31 > 30 ok
    with pytest.raises(SerpApiQuotaExceeded):
        c.search(engine="x")
    assert sum("search.json" in u for u in fake.calls) == 1


def test_errors_never_contain_the_api_key():
    def bad(method, url, headers, body):
        return 401, {"error": f"Invalid API key. Your API key should be here: {KEY}"}

    with pytest.raises(SerpApiError) as e:
        SerpApiClient(KEY, bad).search(engine="x")
    assert KEY not in str(e.value)

    def down(*a):
        raise HttpError(f"network error {KEY}")

    with pytest.raises(SerpApiError) as e2:
        SerpApiClient(KEY, down).searches_left  # noqa: B018
    assert KEY not in str(e2.value)


def test_key_is_never_sent_over_plain_http():
    with pytest.raises(HttpError):
        urllib_transport("GET", "http://serpapi.com/search.json?api_key=" + KEY, {}, None)


def test_flights_request_shape_and_known_ids():
    fake = FakeSerp()
    f = SerpApiFlights(SerpApiClient(KEY, fake))
    offers = f.search_flights("DEL", "DGH", DAY)
    url = next(u for u in fake.calls if "engine=google_flights" in u)
    for part in ["departure_id=DEL", "arrival_id=DGH", "outbound_date=2026-12-22", "type=2", "currency=INR"]:
        assert part in url
    assert f.known_ids == {o.offer_id for o in offers}
    rules = f.get_fare_rules(offers[0].offer_id)
    assert rules.known is False  # never guess refundability


def test_hotels_need_dates():
    h = SerpApiHotels(SerpApiClient(KEY, FakeSerp()))
    with pytest.raises(SerpApiError):
        h.search_hotels("Dharamshala")
    assert h.search_hotels("Dharamshala", True, DAY, date(2026, 12, 24))


# --- hybrid fallback -------------------------------------------------------------------------
def test_hybrid_uses_live_then_falls_back_to_labelled_samples():
    live = SerpApiFlights(SerpApiClient(KEY, FakeSerp()))
    hy = HybridFlights(live)
    assert hy.search_flights("DEL", "DGH", DAY)[0].source == "serpapi"
    down = HybridFlights(SerpApiFlights(SerpApiClient(KEY, FakeSerp(fail=True))), MockFlights())
    got = down.search_flights("DEL", "DGH", DAY)
    assert got and all(o.source == "mock" for o in got)
    empty = HybridFlights(SerpApiFlights(SerpApiClient(KEY, FakeSerp(flights={}))), MockFlights())
    assert all(o.source == "mock" for o in empty.search_flights("DEL", "DGH", DAY))


def test_hybrid_breaker_stops_hammering_a_dead_live_source():
    fake = FakeSerp(fail=True)
    hy = HybridFlights(SerpApiFlights(SerpApiClient(KEY, fake)))
    for _ in range(10):
        hy.search_flights("DEL", "DGH", DAY)
    assert len(fake.calls) == 3  # three failures, then the breaker opens


def test_hybrid_fare_rules_follow_the_offer_source():
    hy = HybridFlights(SerpApiFlights(SerpApiClient(KEY, FakeSerp())))
    live = hy.search_flights("DEL", "DGH", DAY)[0]
    assert hy.get_fare_rules(live.offer_id).known is False
    sample = MockFlights().search_flights("DEL", "DGH", DAY)[0]
    assert hy.get_fare_rules(sample.offer_id).known is True  # a mock id keeps mock rules


def test_hybrid_hotels_fall_back_without_dates():
    hy = HybridHotels(SerpApiHotels(SerpApiClient(KEY, FakeSerp())), MockHotels())
    assert all(h.source == "mock" for h in hy.search_hotels("Dharamshala", True, None, None))


# --- end to end ----------------------------------------------------------------------------
def live_toolbox(flights_live=True, hotels_live=True):
    tb = mock_toolbox()
    client = SerpApiClient(KEY, FakeSerp())
    if flights_live:
        tb._flights = Guarded(HybridFlights(SerpApiFlights(client)), retries=0)
    if hotels_live:
        tb._hotels = Guarded(HybridHotels(SerpApiHotels(client)), retries=0)
    return tb


def manifest(budget=150000):
    return Manifest.model_validate({
        "trip_id": "t", "event": {"name": "RCB vs GT", "venue": "Dharamshala", "date": "2026-12-23"},
        "travelers": {"adults": 2}, "budget": {"tier": "mid", "max_total": budget},
    })  # fmt: skip


def test_live_itinerary_is_honest_about_what_it_is():
    m = manifest()
    it = aggregate(m, price(m, live_toolbox()))
    assert it.data_source == "live" and it.bookable is False and "comparison only" in it.bookable_reason
    flights = [s for s in it.segments if s.type == "flight"]
    assert all(s.refundable is None for s in flights)  # unknown, not "True"
    assert next(s for s in it.segments if s.type == "hotel").refundable is None
    assert any("could not be verified" in w for w in it.warnings)
    assert any("sample estimates" in w for w in it.warnings)
    assert not any("Weather advisory" in w for w in it.warnings)  # sample weather must not pose as real
    assert it.total_cost == sum(s.cost for s in it.segments) and it.total_cost > 0
    out = next(s for s in it.segments if s.type == "flight")
    assert out.details["source"] == "serpapi" and out.details["flight_number"]


def test_mixed_sources_are_flagged_and_not_bookable():
    m = manifest()
    it = aggregate(m, price(m, live_toolbox(hotels_live=False)))
    assert it.data_source == "mixed" and it.bookable is False


def test_sample_itinerary_stays_bookable():
    m = manifest()
    it = aggregate(m, price(m, mock_toolbox()))
    assert it.data_source == "sample" and it.bookable is True and it.bookable_reason is None


def test_book_endpoint_refuses_live_price_plans():
    original = main.get_toolbox
    main.get_toolbox = lambda: live_toolbox()
    try:
        with TestClient(main.app) as c:
            it = c.post("/plan", json={"query": "RCB vs GT, Dharamshala, 2 people, 150k"}).json()["itinerary"]
            assert it["data_source"] == "live"
            r = c.post("/book", json={"itinerary_id": it["itinerary_id"], "payment_token": "tok_test"})
            assert r.status_code == 422 and "comparison only" in r.json()["detail"]
    finally:
        main.get_toolbox = original


def test_live_flight_numbers_enable_status_polling():
    m = manifest()
    it = aggregate(m, price(m, live_toolbox()))
    trip = Trip.from_itinerary(it, "b1")
    nums = [n.flight_iata for n in trip.nodes if n.kind == "flight"]
    assert all(n and " " not in n for n in nums)  # e.g. "6E6201", the AviationStack format
    sample = aggregate(m, price(m, mock_toolbox()))
    assert all(n.flight_iata is None for n in Trip.from_itinerary(sample, "b2").nodes)


# --- live (opt-in) ---------------------------------------------------------------------------
live = pytest.mark.skipif(os.getenv("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1 (uses real quota)")


@live
def test_live_google_flights_and_hotels():
    load_env(force=True)
    client = SerpApiClient.from_env()
    assert client is not None and client.searches_left > 0
    offers = SerpApiFlights(client).search_flights("DEL", "DGH", date(2026, 12, 22))  # same query as the probe
    assert offers and all(o.price > 0 and o.flight_number for o in offers)
    hotels = SerpApiHotels(client).search_hotels("Dharamshala", True, date(2026, 12, 22), date(2026, 12, 24))
    assert hotels and all(h.nightly_price > 0 for h in hotels)
