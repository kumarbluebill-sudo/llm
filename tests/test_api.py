import json

from fastapi.testclient import TestClient

from api.main import app
from api.schemas import Manifest

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_plan_returns_manifest():
    r = client.post("/plan", json={"query": "RCB vs GT, Dharamshala, 2 people, 25k"})
    body = r.json()
    assert r.status_code == 200 and body["status"] == "ok"
    assert body["manifest"]["event"]["venue"] == "Dharamshala"
    it = body["itinerary"]
    assert it["total_cost"] > 0 and len(it["segments"]) == 6


def _events(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        ev, data = block.split("\n", 1)
        out.append((ev.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return out


def test_stream_emits_stages_in_order():
    r = client.get("/plan/stream", params={"query": "RCB vs GT, Dharamshala, 2 people, 25k"})
    assert r.headers["content-type"].startswith("text/event-stream")
    names = [e for e, _ in _events(r.text)]
    assert names == ["stage", "manifest", "stage", "itinerary", "done"]


def test_stream_without_destination_skips_pricing():
    r = client.get("/plan/stream", params={"query": "somewhere nice, 20k"})
    assert [e for e, _ in _events(r.text)] == ["stage", "manifest", "done"]


def test_stream_rejects_short_query():
    assert client.get("/plan/stream", params={"query": "x"}).status_code == 422


def test_plan_without_destination_has_no_itinerary():
    r = client.post("/plan", json={"query": "somewhere nice, 20k"})
    assert r.status_code == 200 and r.json()["itinerary"] is None


def test_plan_rejects_short_query():
    assert client.post("/plan", json={"query": "x"}).status_code == 422


def test_manifest_validates_doc_example():
    m = Manifest.model_validate({
        "trip_id": "t1",
        "event": {"name": "RCB vs GT", "venue": "Dharamshala", "date": "2026-04-15"},
        "budget": {"tier": "mid", "max_total": 25000},
    })
    assert m.constraints.refundable_only is True


# --- booking ---------------------------------------------------------------
def _wait_for(c, bid, pred, tries=100):
    import time

    for _ in range(tries):
        st = c.get(f"/booking/{bid}").json()
        if pred(st):
            return st
        time.sleep(0.02)
    raise AssertionError(f"timeout; last={st['state']}")


def _plan(c, q="RCB vs GT, Dharamshala, 2 people, 150k"):
    return c.post("/plan", json={"query": q}).json()["itinerary"]


def test_book_happy_path_via_api():
    with TestClient(app) as c:
        it = _plan(c)
        r = c.post("/book", json={"itinerary_id": it["itinerary_id"], "payment_token": "tok_test"})
        assert r.status_code == 202
        bid = r.json()["booking_id"]
        st = _wait_for(c, bid, lambda s: s["done"])
        assert st["state"] == "TICKETED"
        assert [e["state"] for e in st["events"] if e["state"]][-1] == "TICKETED"


def test_book_unknown_or_expired_quote_is_410():
    with TestClient(app) as c:
        r = c.post("/book", json={"itinerary_id": "nope", "payment_token": "tok_test"})
        assert r.status_code == 410


def test_book_rejects_over_budget_itinerary():
    with TestClient(app) as c:
        it = _plan(c, "RCB vs GT, Dharamshala, 2 people, 5k")
        assert it["within_budget"] is False
        r = c.post("/book", json={"itinerary_id": it["itinerary_id"], "payment_token": "tok_test"})
        assert r.status_code == 422


def test_client_cannot_supply_prices():
    with TestClient(app) as c:
        it = _plan(c)
        r = c.post(
            "/book",
            json={"itinerary_id": it["itinerary_id"], "payment_token": "tok_test", "total": 1},
        )
        assert r.status_code == 202  # extra fields ignored; quote comes from the server


def test_booking_unknown_is_404_and_approval_conflict_is_409():
    with TestClient(app) as c:
        assert c.get("/booking/missing").status_code == 404
        it = _plan(c)
        bid = c.post(
            "/book", json={"itinerary_id": it["itinerary_id"], "payment_token": "tok_test"}
        ).json()["booking_id"]
        _wait_for(c, bid, lambda s: s["done"])
        assert c.post(f"/booking/{bid}/approval", json={"decision": "approved"}).status_code == 409
        assert c.post(f"/booking/{bid}/approval", json={"decision": "maybe"}).status_code == 422


# --- disruption --------------------------------------------------------------
def _booked(c, q="RCB vs GT, Dharamshala, 2 people, 150k"):
    it = _plan(c, q)
    bid = c.post("/book", json={"itinerary_id": it["itinerary_id"], "payment_token": "tok_test"}).json()["booking_id"]
    _wait_for(c, bid, lambda s: s["done"])
    return bid


def test_disrupt_cancellation_returns_held_options_then_choose():
    with TestClient(app) as c:
        bid = _booked(c)
        r = c.post(f"/disrupt/{bid}", json={"type": "CANCELLATION", "notice_hours": 3})
        assert r.status_code == 200
        case = r.json()
        assert case["severity"] == "CRITICAL" and case["status"] == "AWAITING_APPROVAL"
        assert case["options"] and all(o["hold_id"] for o in case["options"])
        assert case["metrics"]["time_to_first_option_s"] < 90
        # DGCA: always refund-or-rebook; cash compensation only if no alternate departs within 2h
        assert any(e["kind"] == "refund_or_rebook" for e in case["entitlements"])
        assert len(case["claims"]) == sum(e["kind"] == "compensation" for e in case["entitlements"])
        got = c.get(f"/disruption/{case['case_id']}").json()
        assert got["case_id"] == case["case_id"]
        done = c.post(f"/disruption/{case['case_id']}/choose", json={"option_id": case["options"][0]["option_id"]})
        assert done.status_code == 200 and done.json()["status"] == "RESOLVED"
        again = c.post(f"/disruption/{case['case_id']}/choose", json={"option_id": case["options"][0]["option_id"]})
        assert again.status_code == 409


def test_disrupt_low_delay_and_dismiss_and_errors():
    with TestClient(app) as c:
        bid = _booked(c)
        low = c.post(f"/disrupt/{bid}", json={"type": "DELAY", "delay_minutes": 20, "flight": "return"}).json()
        assert low["severity"] == "LOW" and low["options"] == []
        case = c.post(f"/disrupt/{bid}", json={"type": "CANCELLATION", "notice_hours": 2}).json()
        assert c.post(f"/disruption/{case['case_id']}/dismiss").json()["status"] == "DISMISSED"
        assert c.post(f"/disruption/{case['case_id']}/choose", json={"option_id": "x"}).status_code == 409
        assert c.post("/disrupt/missing", json={"type": "DELAY"}).status_code == 404
        assert c.get("/disruption/missing").status_code == 404
