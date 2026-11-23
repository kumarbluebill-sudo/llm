"""Runs only when POSTGRES_URL points at a database (e.g. `docker compose up postgres`)."""
import os
import uuid

import pytest

from saga.store import PostgresStore, latest_state

pytestmark = pytest.mark.skipif(not os.getenv("POSTGRES_URL"), reason="POSTGRES_URL not set")


def test_events_are_ordered_and_append_only():
    import psycopg

    url = os.environ["POSTGRES_URL"]
    store = PostgresStore(url)
    bid = f"t-{uuid.uuid4().hex[:8]}"
    store.append(bid, "STATE", "QUOTED", {"a": 1})
    store.append(bid, "STEP_STARTED", None, {"step": "hold_flight"})
    store.append(bid, "STATE", "HELD")
    ev = store.events(bid)
    assert [e["type"] for e in ev] == ["STATE", "STEP_STARTED", "STATE"]
    assert latest_state(ev) == "HELD" and ev[0]["data"] == {"a": 1}
    with psycopg.connect(url, autocommit=True) as c:
        with pytest.raises(psycopg.errors.RaiseException):
            c.execute("UPDATE booking_events SET state='X' WHERE booking_id=%s", (bid,))
        with pytest.raises(psycopg.errors.RaiseException):
            c.execute("DELETE FROM booking_events WHERE booking_id=%s", (bid,))


def test_idempotency_upsert():
    store = PostgresStore(os.environ["POSTGRES_URL"])
    key = f"k-{uuid.uuid4().hex[:8]}"
    assert store.get_idem(key) is None
    store.put_idem(key, "IN_PROGRESS")
    store.put_idem(key, "DONE", {"ok": 1})
    assert store.get_idem(key) == {"status": "DONE", "result": {"ok": 1}}


def test_booking_ids_lists_every_booking():
    store = PostgresStore(os.environ["POSTGRES_URL"])
    a, b = f"t-{uuid.uuid4().hex[:8]}", f"t-{uuid.uuid4().hex[:8]}"
    store.append(a, "STATE", "QUOTED")
    store.append(b, "STATE", "QUOTED")
    assert {a, b} <= set(store.booking_ids())
