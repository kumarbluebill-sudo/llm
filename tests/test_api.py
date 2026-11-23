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


def test_plan_rejects_short_query():
    assert client.post("/plan", json={"query": "x"}).status_code == 422


def test_manifest_validates_doc_example():
    m = Manifest.model_validate({
        "trip_id": "t1",
        "event": {"name": "RCB vs GT", "venue": "Dharamshala", "date": "2026-04-15"},
        "budget": {"tier": "mid", "max_total": 25000},
    })
    assert m.constraints.refundable_only is True
