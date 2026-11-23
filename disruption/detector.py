"""DETECT: turn webhooks, status polls and emails into DisruptionSignals. Emails go through an
LLM, but the result is validated and must match a flight the traveller actually holds."""
import json
import uuid
from datetime import datetime

from pydantic import BaseModel, ValidationError

from agents.llm import LLMClient
from agents.planner import extract_json
from disruption.models import DisruptionSignal, SignalType, Trip

_WEBHOOK_TYPES = {
    "FLIGHT_CANCELLED": SignalType.CANCELLATION,
    "FLIGHT_DELAYED": SignalType.DELAY,
    "MISSED_CONNECTION": SignalType.MISSED_CONNECTION,
    "DENIED_BOARDING": SignalType.DENIED_BOARDING,
}
_STATUS_TYPES = {"CANCELLED": SignalType.CANCELLATION, "DELAYED": SignalType.DELAY}


def from_webhook(payload: dict, now: datetime) -> DisruptionSignal | None:
    """NDC-style push: {"event": "FLIGHT_DELAYED", "flight": ref, "delay_min": 95, ...}."""
    kind = _WEBHOOK_TYPES.get(str(payload.get("event")))
    if kind is None or "flight" not in payload:
        return None
    return DisruptionSignal(
        signal_id=str(uuid.uuid4()),
        type=kind,
        flight_ref=str(payload["flight"]),
        delay_minutes=int(payload.get("delay_min", 0)),
        notice_hours=payload.get("notice_hours"),
        source="webhook",
        occurred_at=datetime.fromisoformat(payload["occurred_at"]) if "occurred_at" in payload else now,
        received_at=now,
    )


def from_status(status: dict, now: datetime, seen: set[str] | None = None) -> DisruptionSignal | None:
    """Polling: {"flight": ref, "status": "DELAYED", "delay_min": 95}. `seen` de-duplicates polls."""
    kind = _STATUS_TYPES.get(str(status.get("status")))
    if kind is None:
        return None
    key = f"{status['flight']}:{status['status']}:{status.get('delay_min', 0)}"
    if seen is not None:
        if key in seen:
            return None
        seen.add(key)
    return DisruptionSignal(
        signal_id=str(uuid.uuid4()),
        type=kind,
        flight_ref=str(status["flight"]),
        delay_minutes=int(status.get("delay_min", 0)),
        notice_hours=status.get("notice_hours"),
        source="poll",
        occurred_at=now,
        received_at=now,
    )


class _EmailExtract(BaseModel):
    type: SignalType | None = None
    flight_ref: str | None = None
    delay_minutes: int = 0
    notice_hours: float | None = None


EMAIL_PROMPT = (
    "Extract a flight disruption from the email. Reply with ONE JSON object: "
    '{"type": "DELAY"|"CANCELLATION"|"MISSED_CONNECTION"|"DENIED_BOARDING"|null, '
    '"flight_ref": string|null, "delay_minutes": int, "notice_hours": number|null}. '
    "Use null if the email is not a disruption notice. Known flight refs: "
)


def from_email(text: str, trips: list[Trip], llm: LLMClient, now: datetime) -> DisruptionSignal | None:
    refs = sorted({n.ref for t in trips for n in t.nodes if n.kind == "flight" and n.ref})
    raw = llm.complete(EMAIL_PROMPT + json.dumps(refs), f"EMAIL:\n{text}")
    try:
        data = _EmailExtract.model_validate(extract_json(raw))
    except (ValueError, TypeError, ValidationError):
        return None
    # Guard against hallucination / prompt injection: the flight must be one we booked.
    if data.type is None or data.flight_ref not in refs or data.delay_minutes < 0:
        return None
    return DisruptionSignal(
        signal_id=str(uuid.uuid4()),
        type=data.type,
        flight_ref=data.flight_ref,
        delay_minutes=data.delay_minutes,
        notice_hours=data.notice_hours,
        source="email",
        occurred_at=now,
        received_at=now,
    )


def match_trips(sig: DisruptionSignal, trips: list[Trip]) -> list[Trip]:
    return [t for t in trips if t.node_for_flight(sig.flight_ref) is not None]
