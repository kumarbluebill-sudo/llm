import base64
import json
import os
from datetime import date

import pytest

from api.config import load_env
from mcp_servers.http import HttpError
from mcp_servers.traveldesk import (
    PROD_BASE,
    TEST_BASE,
    LiveBookingDisabled,
    ReserveRequest,
    TravelDeskClient,
    TravelDeskError,
    environment_of,
    parse_offer,
    token_issuer,
)


def make_token(iss: str, secret="sig-should-never-leak") -> str:
    def enc(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'HS256', 'typ': 'JWT'})}.{enc({'iss': iss, 'aud': iss, 'AgencyId': '1'})}.{secret}"


TEST_TOKEN, PROD_TOKEN = make_token("test.traveldesk.me"), make_token("api.traveldesk.me")

OFFER = {"id": 10, "name": "Dubai fishing (4 hours)", "tourId": 4, "tourName": "Fishing",
         "prices": [{"currency": "aed", "recPricePerAdult": 52.5, "recPricePerChild": 31.5, "recPricePerTrip": 1825}]}  # fmt: skip


class Fake:
    def __init__(self, status=200, data=None):
        self.calls, self.status, self.data = [], status, data if data is not None else {}

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        return self.status, self.data


def client(token=TEST_TOKEN, fake=None, **kw):
    fake = fake or Fake()
    return TravelDeskClient(token, transport=fake, **kw), fake


# --- environment & host safety ---------------------------------------------------
def test_environment_comes_from_the_issuer_claim():
    assert token_issuer(TEST_TOKEN) == "test.traveldesk.me"
    assert environment_of(TEST_TOKEN) == "test" and environment_of(PROD_TOKEN) == "production"
    assert environment_of("not-a-jwt") == "unknown" and token_issuer("a.b.c") is None


def test_default_base_follows_the_token_environment():
    assert client(TEST_TOKEN)[0].base == TEST_BASE and client(PROD_TOKEN)[0].base == PROD_BASE


def test_a_key_is_never_sent_to_a_mismatched_host():
    with pytest.raises(TravelDeskError, match="does not match"):
        TravelDeskClient(TEST_TOKEN, base=PROD_BASE)
    with pytest.raises(TravelDeskError, match="does not match"):
        TravelDeskClient(PROD_TOKEN, base=TEST_BASE)


# --- reads ---------------------------------------------------------------------------
def test_reads_send_bearer_and_expected_params():
    c, fake = client(fake=Fake(data=[OFFER]))
    offers = c.offers(include_prices=True, country_id=2)
    method, url, headers, body = fake.calls[0]
    assert (method, body) == ("GET", None)
    assert url == f"{TEST_BASE}/offers?IncludePrices=true&CountryId=2"
    assert headers["Authorization"] == f"Bearer {TEST_TOKEN}"
    assert offers[0].name == "Dubai fishing (4 hours)" and offers[0].per_adult == 52.5 and offers[0].currency == "aed"


def test_availability_and_other_read_urls():
    c, fake = client()
    c.availability(10, date(2026, 12, 1), date(2026, 12, 7), "AED")
    c.balance()
    c.tours(currency="AED")
    urls = [u for _, u, _, _ in fake.calls]
    assert urls[0].endswith("/availability/byoffer?Id=10&From=2026-12-01&To=2026-12-07&CurrencyCode=AED")
    assert urls[1].endswith("/balance") and urls[2].endswith("/tours?CurrencyCode=AED")


def test_parse_offer_is_tolerant():
    assert parse_offer({"id": 1}) is None and parse_offer({"name": "x"}) is None
    assert parse_offer({"id": 2, "name": "No prices"}).per_adult is None


def test_errors_never_contain_the_key():
    c, _ = client(fake=Fake(status=401, data={"detail": f"bad {TEST_TOKEN}"}))
    with pytest.raises(TravelDeskError) as e:
        c.balance()
    assert TEST_TOKEN not in str(e.value) and "401" in str(e.value)

    def down(*a):
        raise HttpError(f"network error {TEST_TOKEN}")

    with pytest.raises(TravelDeskError) as e2:
        TravelDeskClient(TEST_TOKEN, transport=down).balance()
    assert TEST_TOKEN not in str(e2.value)


# --- booking gate ------------------------------------------------------------------------
def req(**kw):
    base = {"offerId": 10, "date": date(2026, 12, 5), "adults": 2, "guestName": "Test Guest",
            "referenceNumber": "bk-123:exp:10", "totalCost": 105.0, "totalCostUsd": 28.8}  # fmt: skip
    return ReserveRequest(**{**base, **kw})


def test_booking_is_disabled_by_default_and_never_hits_the_network():
    c, fake = client()
    for call in (lambda: c.reserve(req()), lambda: c.confirm(5), lambda: c.cancel(5)):
        with pytest.raises(LiveBookingDisabled):
            call()
    assert fake.calls == []


def test_booking_allowed_on_test_environment_when_opted_in():
    c, fake = client(fake=Fake(data={"id": 77}), allow_booking=True)
    assert c.reserve(req())["id"] == 77
    c.confirm(77)
    c.cancel(77)
    methods = [(m, u.rsplit("/", 1)[-1], b) for m, u, _, b in fake.calls]
    assert methods[0][:2] == ("POST", "reserve") and methods[1] == ("POST", "confirm", {"id": 77})
    assert methods[2] == ("POST", "cancel", {"id": 77})


def test_production_booking_needs_two_explicit_opt_ins(monkeypatch):
    c, _ = client(PROD_TOKEN, allow_booking=True)
    monkeypatch.delenv("APP_ENV", raising=False)
    with pytest.raises(LiveBookingDisabled, match="production"):
        c.cancel(1)
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(LiveBookingDisabled):
        c.cancel(1)  # APP_ENV alone is not enough
    monkeypatch.setenv("TRAVELDESK_ALLOW_PRODUCTION", "1")
    c.cancel(1)


def test_reserve_payload_uses_the_documented_field_names():
    p = req(email="a@b.co", payByBalance=10).payload()
    assert p["offerId"] == 10 and p["guestName"] == "Test Guest" and p["referenceNumber"] == "bk-123:exp:10"
    assert p["date"] == "2026-12-05" and p["totalCostUsd"] == 28.8 and p["children"] == 0 and "phone" not in p


def test_reserve_validation():
    for bad in ({"adults": 0}, {"totalCost": 0}, {"referenceNumber": "x"}, {"guestName": "A"}):
        with pytest.raises(ValueError):
            req(**bad)


def test_from_env_reads_the_booking_flag(monkeypatch):
    monkeypatch.delenv("TRAVELDESK_API_KEY", raising=False)
    assert TravelDeskClient.from_env() is None
    monkeypatch.setenv("TRAVELDESK_API_KEY", TEST_TOKEN)
    monkeypatch.setenv("TRAVELDESK_ALLOW_LIVE_BOOKING", "false")
    with pytest.raises(LiveBookingDisabled):
        TravelDeskClient.from_env(transport=Fake()).cancel(1)
    monkeypatch.setenv("TRAVELDESK_ALLOW_LIVE_BOOKING", "true")
    assert TravelDeskClient.from_env(transport=Fake()).cancel(1) == {}


live = pytest.mark.skipif(os.getenv("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1")


@live
def test_live_read_only_calls_against_the_test_api():
    load_env(force=True)
    c = TravelDeskClient.from_env()
    assert c is not None and c.environment == "test" and c.base == TEST_BASE
    assert {"balance", "creditFacility"} <= set(c.balance())
    offers = c.offers()
    assert offers and all(o.name for o in offers)
