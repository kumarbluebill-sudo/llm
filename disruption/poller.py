"""Poll live flight status for the flights a trip holds and turn problems into signals.
Only nodes that carry a real `flight_iata` are polled; our mock offers do not have one."""
from datetime import datetime

from disruption import detector
from disruption.models import DisruptionSignal, Trip
from mcp_servers.aviationstack import AviationStackClient, AviationStackError, FlightStatus

DELAY_THRESHOLD_MIN = 15  # smaller delays are noise; triage still decides what matters


def status_to_poll_record(fs: FlightStatus, flight_ref: str, now: datetime) -> dict | None:
    """Shape a FlightStatus like the generic poll record that detector.from_status understands."""
    notice = None
    if fs.scheduled_departure is not None:
        sched = fs.scheduled_departure
        notice = (sched - now).total_seconds() / 3600 if sched.tzinfo else None
    if fs.status == "cancelled":
        return {"flight": flight_ref, "status": "CANCELLED", "notice_hours": notice}
    if fs.delay_min >= DELAY_THRESHOLD_MIN:
        return {"flight": flight_ref, "status": "DELAYED", "delay_min": fs.delay_min, "notice_hours": notice}
    return None


def poll_trip(
    trip: Trip, client: AviationStackClient, now: datetime, seen: set[str] | None = None
) -> tuple[list[DisruptionSignal], list[str]]:
    """Returns (new signals, problems). A failing lookup never raises: polling must not break."""
    signals: list[DisruptionSignal] = []
    problems: list[str] = []
    for node in trip.nodes:
        if node.kind != "flight" or not node.flight_iata or not node.ref:
            continue
        try:
            fs = client.flight_status(node.flight_iata)
        except AviationStackError as e:
            problems.append(f"{node.flight_iata}: {e}")
            continue
        if fs is None:
            problems.append(f"{node.flight_iata}: no live record")
            continue
        record = status_to_poll_record(fs, node.ref, now)
        sig = detector.from_status(record, now, seen) if record else None
        if sig:
            signals.append(sig)
    return signals, problems
