from fastapi.testclient import TestClient

from agents.llm import MockLLM
from api import main
from api.main import app
from api.safety import looks_like_pan, redact_pan
from disruption.models import SignalType
from tests.test_disruption import NOW, World

CARD = "4111 1111 1111 1111"


def test_pan_detection_uses_luhn():
    assert looks_like_pan(CARD) and looks_like_pan("4111-1111-1111-1111") and looks_like_pan("5500000000000004")
    for ok in ["150000", "9876543210", "2026-12-23", "1234 5678 9012 3456", "phone 9876543210 budget 25000"]:
        assert not looks_like_pan(ok), ok


def test_redaction_keeps_ordinary_numbers():
    out = redact_pan(f"RCB vs GT 25000 for 2, card {CARD}, phone 9876543210")
    assert "4111" not in out and "[REDACTED]" in out and "25000" in out and "9876543210" in out


def test_card_number_never_echoed_or_sent_to_llm():
    seen = []

    class Spy(MockLLM):
        def complete(self, system, user):
            seen.append(user)
            return super().complete(system, user)

    main.get_llm = lambda: Spy()
    try:
        with TestClient(app) as c:
            r = c.post("/plan", json={"query": f"RCB vs GT Dharamshala 25k card {CARD}"})
            s = c.get("/plan/stream", params={"query": f"RCB vs GT Dharamshala 25k {CARD}"})
    finally:
        main.get_llm = lambda: MockLLM()
    assert "4111" not in r.text and "4111" not in s.text
    assert seen and all("4111" not in u for u in seen)


def test_invalid_user_input_is_422_but_llm_outage_is_503():
    class Broken:
        def complete(self, system, user):
            raise ConnectionError("provider down")

    class Nonsense:
        def complete(self, system, user):
            return "no json here"

    original = main.get_llm
    try:
        with TestClient(app) as c:
            main.get_llm = lambda: Broken()
            assert c.post("/plan", json={"query": "RCB vs GT Dharamshala 25k"}).status_code == 503
            main.get_llm = lambda: Nonsense()
            assert c.post("/plan", json={"query": "RCB vs GT Dharamshala 25k"}).status_code == 422
    finally:
        main.get_llm = original


def test_oversized_query_rejected():
    with TestClient(app) as c:
        assert c.post("/plan", json={"query": "a" * 501}).status_code == 422
        assert c.get("/plan/stream", params={"query": "a" * 501}).status_code == 422


def test_same_quote_cannot_be_booked_twice():
    with TestClient(app) as c:
        it = c.post("/plan", json={"query": "RCB vs GT, Dharamshala, 2 people, 150k"}).json()["itinerary"]
        body = {"itinerary_id": it["itinerary_id"], "payment_token": "tok_test"}
        a, b = c.post("/book", json=body).json(), c.post("/book", json=body).json()
        assert a["booking_id"] == b["booking_id"] and b["duplicate"] is True


def test_admin_reconcile_endpoint_works_with_token(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "t0k")
    with TestClient(app) as c:
        assert c.post("/admin/reconcile").status_code == 403
        r = c.post("/admin/reconcile", headers={"x-admin-token": "t0k"})
        assert r.status_code == 200 and {"checked", "counts", "findings"} <= set(r.json())


# --- tracing ---------------------------------------------------------------
def test_spans_cover_planner_suppliers_saga_and_disruption(spans):
    w = World()  # plans prices and books a trip
    w.mgr.handle(w.signal(SignalType.CANCELLATION))
    names = {s.name for s in spans.get_finished_spans()}
    assert {"supplier.call", "saga.step", "disruption.handle", "disruption.options", "disruption.notify"} <= names
    handle = next(s for s in spans.get_finished_spans() if s.name == "disruption.handle")
    assert handle.attributes["severity"] == "CRITICAL" and handle.attributes["options"] >= 1


def test_planner_span_and_cache_hit_attribute(spans):
    from agents.planner import plan
    from mcp_servers.toolbox import mock_toolbox

    plan("RCB vs GT, Dharamshala, 2 people, 25k", MockLLM())
    p = next(s for s in spans.get_finished_spans() if s.name == "planner.plan")
    assert p.attributes["attempts"] == 1
    tools = mock_toolbox()
    from datetime import date

    tools.search_flights("DEL", "DGH", date(2027, 4, 14))
    tools.search_flights("DEL", "DGH", date(2027, 4, 14))
    hits = [s.attributes["cache_hit"] for s in spans.get_finished_spans() if s.name == "supplier.call"]
    assert hits[-2:] == [False, True]


def test_spans_never_contain_user_text_or_payment_data(spans):
    w = World()
    w.mgr.handle(w.signal(SignalType.DELAY, delay=30, notice=None))
    from agents.planner import plan

    plan("RCB vs GT, Dharamshala, 2 people, 25k my secret note", MockLLM())
    blob = json.dumps([dict(s.attributes) for s in spans.get_finished_spans()], default=str)
    assert "secret note" not in blob and "tok_test" not in blob and "RCB vs GT" not in blob
    assert NOW  # keep import used


import json
