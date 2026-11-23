"""Append-only booking event log + idempotency table. In-memory for tests/dev, Postgres for prod."""
import json
import threading
from datetime import UTC, datetime
from typing import Any, Protocol


class BookingStore(Protocol):
    def append(self, booking_id: str, type: str, state: str | None = None, data: dict | None = None) -> None: ...
    def events(self, booking_id: str) -> list[dict]: ...
    def booking_ids(self) -> list[str]: ...
    def get_idem(self, key: str) -> dict | None: ...
    def put_idem(self, key: str, status: str, result: Any = None) -> None: ...


def latest_state(events: list[dict]) -> str | None:
    for e in reversed(events):
        if e["state"]:
            return e["state"]
    return None


class InMemoryStore:
    def __init__(self) -> None:
        self._events: dict[str, list[dict]] = {}
        self._idem: dict[str, dict] = {}
        self._lock = threading.Lock()

    def append(self, booking_id, type, state=None, data=None):
        with self._lock:
            log = self._events.setdefault(booking_id, [])
            log.append(
                {
                    "seq": len(log) + 1,
                    "type": type,
                    "state": state,
                    "data": data or {},
                    "at": datetime.now(UTC).isoformat(),
                }
            )

    def events(self, booking_id):
        with self._lock:
            return [dict(e) for e in self._events.get(booking_id, [])]

    def booking_ids(self):
        with self._lock:
            return list(self._events)

    def get_idem(self, key):
        with self._lock:
            rec = self._idem.get(key)
            return dict(rec) if rec else None

    def put_idem(self, key, status, result=None):
        with self._lock:
            self._idem[key] = {"status": status, "result": result}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS booking_events (
    id BIGSERIAL PRIMARY KEY,
    booking_id TEXT NOT NULL,
    type TEXT NOT NULL,
    state TEXT,
    data JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS booking_events_booking ON booking_events (booking_id, id);
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    result JSONB,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE OR REPLACE FUNCTION booking_events_immutable() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'booking_events is append-only'; END; $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS booking_events_no_change ON booking_events;
CREATE TRIGGER booking_events_no_change BEFORE UPDATE OR DELETE ON booking_events
    FOR EACH ROW EXECUTE FUNCTION booking_events_immutable();
"""


class PostgresStore:
    def __init__(self, url: str) -> None:
        import psycopg  # lazy: only needed when Postgres is configured

        self._psycopg = psycopg
        self._url = url
        with self._conn() as c:
            c.execute(_SCHEMA)

    def _conn(self):
        return self._psycopg.connect(self._url, autocommit=True)

    def append(self, booking_id, type, state=None, data=None):
        with self._conn() as c:
            c.execute(
                "INSERT INTO booking_events (booking_id, type, state, data) VALUES (%s,%s,%s,%s)",
                (booking_id, type, state, json.dumps(data or {})),
            )

    def events(self, booking_id):
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, type, state, data, created_at FROM booking_events "
                "WHERE booking_id=%s ORDER BY id",
                (booking_id,),
            ).fetchall()
        return [
            {"seq": i + 1, "type": r[1], "state": r[2], "data": r[3], "at": r[4].isoformat()}
            for i, r in enumerate(rows)
        ]

    def booking_ids(self):
        with self._conn() as c:
            rows = c.execute("SELECT DISTINCT booking_id FROM booking_events ORDER BY booking_id").fetchall()
        return [r[0] for r in rows]

    def get_idem(self, key):
        with self._conn() as c:
            row = c.execute("SELECT status, result FROM idempotency WHERE key=%s", (key,)).fetchone()
        return {"status": row[0], "result": row[1]} if row else None

    def put_idem(self, key, status, result=None):
        with self._conn() as c:
            c.execute(
                "INSERT INTO idempotency (key, status, result) VALUES (%s,%s,%s) "
                "ON CONFLICT (key) DO UPDATE SET status=EXCLUDED.status, result=EXCLUDED.result, "
                "updated_at=now()",
                (key, status, json.dumps(result)),
            )
