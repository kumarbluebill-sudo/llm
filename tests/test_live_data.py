"""Viator / AviationStack / env-file behaviour, using fake transports (no network, no keys).
Opt-in live checks at the bottom run only with RUN_LIVE_TESTS=1."""
import os
from datetime import UTC, datetime, timedelta

import pytest

from agents.aggregator import aggregate
from agents.pricer import price
from api.config import _parse, load_env
from api.schemas import Manifest
from disruption import poller
from disruption.models import SignalType
from mcp_servers.aviationstack import (
    AviationStackClient,
    AviationStackError,
    QuotaExceeded,
    parse_flight,
)
from mcp_servers.http import HttpError, urllib_transport
from mcp_servers.mock_suppliers import (
    MockActivities,
    MockFlights,
    MockHotels,
    MockTransport,
    MockWeather,
)
from mcp_servers.resilience import Guarded
from mcp_servers.toolbox import Toolbox, mock_toolbox
from mcp_servers.viator import (
    PRODUCTION,
    SANDBOX,
    ViatorActivities,
    ViatorError,
    parse_product,
    pick_credentials,
)
from tests.test_disruption import NOW, World

KEY = "k-secret-1234567890"


# --- env files ---------------------------------------------------------------
def test_env_parser_handles_quotes_comments_export_and_bom():
    text = '﻿# c\nA="x y"\nexport B=2\n  C = \'3\' \nbad line\nEMPTY=\n'
    assert _parse(text) == {"A": "x y", "B": "2", "C": "3", "EMPTY": ""}


def test_load_env_wins_order_and_skips_empty(tmp_path, monkeypatch):
    (tmp_path / "env.local").write_text("FOO_T=from_file\nBAR_T=\nREAL_T=file\n")
    monkeypatch.setenv("REAL_T", "real")
    monkeypatch.delenv("FOO_T", raising=False)
    monkeypatch.delenv("BAR_T", raising=False)
    assert load_env(tmp_path, force=True) == ["env.local"]
    assert os.environ["FOO_T"] == "from_file" and "BAR_T" not in os.environ
    assert os.environ["REAL_T"] == "real"  # real environment wins
    monkeypatch.delenv("FOO_T")


def test_tests_never_load_real_env_file(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("SHOULD_NOT_LOAD=1\n")
    assert load_env(tmp_path) == [] and "SHOULD_NOT_LOAD" not in os.environ


def test_transport_refuses_plain_http():
    with pytest.raises(HttpError):
        urllib_transport("GET", "http://example.com/?access_key=" + KEY, {}, None)


# --- viator ---------------------------------------------------------------------
PRODUCT = {
    "productCode": "19016P10", "title": "Hiking Day Tour to Triund", "productUrl": "https://www.viator.com/tours/x/d1-19016P10",
    "pricing": {"currency": "INR", "summary": {"fromPrice": 2920.99}},
    "duration": {"fixedDurationInMinutes": 480},
    "reviews": {"combinedAverageRating": 4.8, "totalReviews": 120},
    "images": [{"variants": [{"url": "https://img.example/a_80.jpg", "width": 80}, {"url": "https://img.example/a_480.jpg", "width": 480}]}],
}
DESTS = {"destinations": [
    {"destinationId": 804, "name": "New Delhi", "type": "CITY"},
    {"destinationId": 25979, "name": "Dharamsala", "type": "CITY"},
    {"destinationId": 1, "name": "Mumbai", "type": "REGION"},
    {"destinationId": 953, "name": "Mumbai", "type": "CITY"},
]}  # fmt: skip


class FakeViator:
    def __init__(self, products=None, search_status=200):
        self.calls, self.products, self.search_status = [], products if products is not None else [PRODUCT], search_status

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if url.endswith("/destinations"):
            return 200, DESTS
        return self.search_status, {"products": self.products} if self.search_status == 200 else {"code": "NOPE"}


def test_parse_product_picks_fields_and_drops_non_https():
    a = parse_product(PRODUCT)
    assert (a.product_code, a.from_price, a.duration_min, a.rating, a.reviews) == ("19016P10", 2921, 480, 4.8, 120)
    assert a.image.endswith("a_480.jpg") and a.url.startswith("https://") and a.source == "viator"
    bad = {**PRODUCT, "productUrl": "http://evil.example/x", "images": [{"variants": [{"url": "javascript:alert(1)", "width": 480}]}]}
    b = parse_product(bad)
    assert b.url is None and b.image is None
    assert parse_product({"productCode": "x", "title": "t"}) is None  # no price -> unusable


def test_viator_maps_our_city_names_and_sends_correct_request():
    fake = FakeViator()
    v = ViatorActivities(KEY, SANDBOX, fake)
    assert v.destination_id("Dharamshala") == 25979 and v.destination_id("Delhi") == 804
    assert v.destination_id("Mumbai") == 953  # prefers the CITY entry
    acts = v.search_activities("Dharamshala", limit=3)
    assert [a.product_code for a in acts] == ["19016P10"]
    method, url, headers, body = fake.calls[-1]
    assert (method, url) == ("POST", SANDBOX + "/products/search")
    assert headers["exp-api-key"] == KEY and "version=2.0" in headers["Accept"]
    assert body == {"filtering": {"destination": "25979"}, "currency": "INR", "pagination": {"start": 1, "count": 3}}
    assert len([c for c in fake.calls if c[1].endswith("/destinations")]) == 1  # looked up once


def test_viator_unknown_city_and_errors_do_not_leak_key():
    v = ViatorActivities(KEY, SANDBOX, FakeViator())
    assert v.search_activities("Atlantis") == []
    bad = ViatorActivities(KEY, SANDBOX, FakeViator(search_status=500))
    with pytest.raises(ViatorError) as e:
        bad.search_activities("Delhi")
    assert KEY not in str(e.value) and "500" in str(e.value)


def test_viator_defaults_to_sandbox_and_production_needs_two_opt_ins():
    env = {"VIATOR_SANDBOX_KEY": "sb", "VIATOR_PRODUCTION_KEY": "pk", "VIATOR_API_ENV": "production"}
    assert pick_credentials(env) == ("sb", SANDBOX)  # env file says production, APP_ENV does not
    assert pick_credentials({**env, "APP_ENV": "production"}) == ("pk", PRODUCTION)
    assert pick_credentials({"VIATOR_API_ENV": "production", "APP_ENV": "production"}) is None  # no key
    assert pick_credentials({}) is None


# --- suggestions in the itinerary ------------------------------------------------------
def _manifest():
    return Manifest.model_validate({
        "trip_id": "t", "event": {"name": "RCB vs GT", "venue": "Dharamshala", "date": "2027-04-15"},
        "travelers": {"adults": 2}, "budget": {"tier": "mid", "max_total": 150000},
    })  # fmt: skip


def test_itinerary_has_ranked_suggestions_outside_the_total():
    m = _manifest()
    it = aggregate(m, price(m, mock_toolbox()))
    assert 1 <= len(it.suggestions) <= 6
    ratings = [s.rating or 0 for s in it.suggestions]
    assert ratings == sorted(ratings, reverse=True)
    assert it.total_cost == sum(s.cost for s in it.segments)  # suggestions never priced in
    assert any("not in the total" in w for w in it.warnings)


def test_suggestions_are_live_viator_data_when_configured():
    tb = mock_toolbox()
    tb._activities = Guarded(ViatorActivities(KEY, SANDBOX, FakeViator()))
    m = _manifest()
    it = aggregate(m, price(m, tb))
    assert [s.code for s in it.suggestions] == ["19016P10"] and it.suggestions[0].source == "viator"


def test_viator_outage_never_breaks_planning():
    tb = mock_toolbox()
    tb._activities = Guarded(ViatorActivities(KEY, SANDBOX, FakeViator(search_status=500)), retries=0, sleep=lambda _: None)
    m = _manifest()
    it = aggregate(m, price(m, tb))
    assert it.suggestions == [] and any("temporarily unavailable" in w for w in it.warnings)
    assert it.total_cost > 0


def test_toolbox_without_activities_returns_empty():
    tb = Toolbox(Guarded(MockFlights()), Guarded(MockHotels()), Guarded(MockTransport()), Guarded(MockWeather()))
    assert tb.search_activities("Delhi") == []
    assert MockActivities().search_activities("Delhi", 2)[0].source == "mock"


# --- aviationstack ----------------------------------------------------------------------------
REC = {
    "flight_date": "2026-11-23", "flight_status": "scheduled",
    "departure": {"iata": "DEL", "scheduled": "2026-11-23T18:30:00+00:00", "delay": 10},
    "arrival": {"iata": "BOM", "scheduled": "2026-11-23T21:15:00+00:00", "delay": None},
    "airline": {"name": "IndiGo"}, "flight": {"iata": "6E329"},
}  # fmt: skip


class FakeAvi:
    def __init__(self, record=REC, status=200, body=None):
        self.calls, self.record, self.status, self.body = [], record, status, body

    def __call__(self, method, url, headers, body):
        self.calls.append(url)
        if self.body is not None:
            return self.status, self.body
        return self.status, {"data": [self.record] if self.record else []}


def test_parse_flight_from_real_shape():
    f = parse_flight(REC)
    assert (f.flight_iata, f.status, f.airline, f.delay_min) == ("6E329", "scheduled", "IndiGo", 10)
    assert parse_flight({"flight_status": "x"}) is None


def test_cache_and_monthly_budget_protect_the_free_quota():
    fake = FakeAvi()
    c = AviationStackClient(KEY, fake, monthly_budget=2, month=lambda: "2026-11")
    c.flight_status("6E329")
    c.flight_status("6E329")  # cached
    assert len(fake.calls) == 1 and c.calls_this_month == 1
    c.flight_status("AI101")
    with pytest.raises(QuotaExceeded):
        c.flight_status("UK999")
    c2 = AviationStackClient(KEY, fake, monthly_budget=1, month=lambda: "2026-12")  # new month, fresh budget
    c2.flight_status("X1")


def test_errors_never_contain_the_api_key():
    for status, body in [(401, {"error": {"code": "invalid_access_key"}}), (200, {"error": {"code": "usage_limit_reached"}})]:
        c = AviationStackClient(KEY, FakeAvi(status=status, body=body))
        with pytest.raises(AviationStackError) as e:
            c.flight_status("6E329")
        assert KEY not in str(e.value)

    def boom(*a):
        raise HttpError("network error: URLError")

    with pytest.raises(AviationStackError) as e:
        AviationStackClient(KEY, boom).flight_status("6E329")
    assert KEY not in str(e.value)


def test_unknown_flight_returns_none():
    assert AviationStackClient(KEY, FakeAvi(record=None)).flight_status("ZZ1") is None


def _trip_with_live_numbers():
    w = World()
    w.trip.node("flight_out").flight_iata = "6E329"
    w.trip.node("flight_return").flight_iata = None  # no real number known
    return w


def test_poller_turns_delay_and_cancellation_into_signals():
    w = _trip_with_live_numbers()
    now = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    delayed = {**REC, "departure": {**REC["departure"], "delay": 95}}
    sigs, problems = poller.poll_trip(w.trip, AviationStackClient(KEY, FakeAvi(delayed)), now)
    assert problems == [] and len(sigs) == 1
    s = sigs[0]
    assert s.type is SignalType.DELAY and s.delay_minutes == 95 and s.flight_ref == w.out_ref and s.source == "poll"
    cancelled = {**REC, "flight_status": "cancelled"}
    sigs, _ = poller.poll_trip(w.trip, AviationStackClient(KEY, FakeAvi(cancelled)), now)
    assert sigs[0].type is SignalType.CANCELLATION
    assert sigs[0].notice_hours == pytest.approx((datetime.fromisoformat(REC["departure"]["scheduled"]) - now).total_seconds() / 3600)


def test_poller_ignores_noise_dedupes_and_never_raises():
    w = _trip_with_live_numbers()
    now = NOW
    assert poller.poll_trip(w.trip, AviationStackClient(KEY, FakeAvi()), now)[0] == []  # 10 min delay = noise
    delayed = {**REC, "departure": {**REC["departure"], "delay": 45}}
    seen: set[str] = set()
    client = AviationStackClient(KEY, FakeAvi(delayed), cache_ttl=0)
    assert len(poller.poll_trip(w.trip, client, now, seen)[0]) == 1
    assert poller.poll_trip(w.trip, client, now, seen)[0] == []  # same status is not a new event
    down = AviationStackClient(KEY, FakeAvi(status=500, body={}))
    sigs, problems = poller.poll_trip(w.trip, down, now)
    assert sigs == [] and len(problems) == 1 and KEY not in problems[0]
    none_node = AviationStackClient(KEY, FakeAvi(record=None))
    assert "no live record" in poller.poll_trip(w.trip, none_node, now)[1][0]


def test_polled_signal_runs_through_the_disruption_pipeline():
    w = _trip_with_live_numbers()
    cancelled = {**REC, "flight_status": "cancelled"}
    sigs, _ = poller.poll_trip(w.trip, AviationStackClient(KEY, FakeAvi(cancelled)), NOW)
    case = w.mgr.handle(sigs[0].model_copy(update={"received_at": NOW}))
    assert case.severity.value in {"HIGH", "CRITICAL"} and case.options
    assert timedelta(0) <= NOW - NOW


# --- live (opt-in) ------------------------------------------------------------------------------
live = pytest.mark.skipif(os.getenv("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1 (uses real keys/quota)")


@live
def test_live_viator_sandbox_returns_dharamshala_activities():
    load_env(force=True)
    creds = pick_credentials()
    assert creds and creds[1] == SANDBOX
    acts = ViatorActivities(*creds).search_activities("Dharamshala", limit=3)
    assert acts and all(a.from_price > 0 and a.source == "viator" for a in acts)


@live
def test_live_aviationstack_parses_a_real_record():
    load_env(force=True)
    client = AviationStackClient.from_env()
    assert client is not None
    fs = client.flight_status("6E329")  # uses 1 of the monthly requests
    assert fs is None or fs.flight_iata == "6E329"
