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
