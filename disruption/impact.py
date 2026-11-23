"""Impact analysis: walk the trip dependency graph and evaluate EVERY downstream node, so a
cancelled flight cannot silently leave a hotel, transfer or event unflagged (cascade miss)."""
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from disruption.models import Trip, TripNode

Status = Literal["ok", "shifted", "at_risk", "broken"]
AT_RISK_SLACK_MIN = 60


class NodeImpact(BaseModel):
    node_id: str
    kind: str
    status: Status
    shift_min: int = 0
    reason: str | None = None


class ImpactReport(BaseModel):
    disrupted: str
    impacts: list[NodeImpact]

    @property
    def broken(self) -> list[str]:
        return [i.node_id for i in self.impacts if i.status == "broken"]

    @property
    def feasible(self) -> bool:
        return not self.broken

    def get(self, node_id: str) -> NodeImpact:
        return next(i for i in self.impacts if i.node_id == node_id)


def descendants(trip: Trip, node_id: str) -> set[str]:
    out: set[str] = set()
    frontier = [node_id]
    while frontier:
        cur = frontier.pop()
        for n in trip.nodes:
            if cur in n.depends_on and n.id not in out:
                out.add(n.id)
                frontier.append(n.id)
    return out


def _topo(trip: Trip) -> list[TripNode]:
    done: list[TripNode] = []
    seen: set[str] = set()
    pending = list(trip.nodes)
    while pending:
        progressed = False
        for n in list(pending):
            if all(d in seen for d in n.depends_on):
                done.append(n)
                seen.add(n.id)
                pending.remove(n)
                progressed = True
        if not progressed:
            raise ValueError("trip graph has a cycle")
    return done


def analyze(
    trip: Trip,
    node_id: str,
    *,
    cancelled: bool = False,
    delay_min: int = 0,
    new_start: datetime | None = None,
    new_end: datetime | None = None,
) -> ImpactReport:
    """Apply a disruption to one node (cancel it, delay it, or replace its times) and report
    the status of every node that depends on it (plus a hotel that may need extending)."""
    target = trip.node(node_id)
    start, end = target.start, target.end
    if new_start is not None and new_end is not None:
        start, end = new_start, new_end
    elif delay_min:
        start, end = start + timedelta(minutes=delay_min), end + timedelta(minutes=delay_min)

    # (start, end, broken?) for every node whose state we have recomputed
    state: dict[str, tuple[datetime, datetime, bool]] = {
        node_id: (start, end, cancelled)
    }
    affected = descendants(trip, node_id)
    impacts: dict[str, NodeImpact] = {}

    for n in _topo(trip):
        if n.id not in affected:
            continue
        deps = []
        for d in n.depends_on:
            ds, de, db = state[d] if d in state else (trip.node(d).start, trip.node(d).end, False)
            # A hotel stay is "ready" at check-in, not at checkout.
            deps.append((ds, ds if trip.node(d).kind == "hotel" else de, db))
        if any(b for _, _, b in deps):
            broken_dep = next(d for d in n.depends_on if d in state and state[d][2])
            state[n.id] = (n.start, n.end, True)
            impacts[n.id] = NodeImpact(
                node_id=n.id, kind=n.kind, status="broken", reason=f"depends on unavailable '{broken_dep}'"
            )
            continue
        gap = timedelta(minutes=n.min_gap_min)
        if n.id == "event":
            gap = timedelta(minutes=trip.event_buffer_min)
        required = max(e for _, e, _ in deps) + gap
        if n.fixed:
            slack = int((n.start - required).total_seconds() // 60)
            if slack < 0:
                status: Status = "broken"
                reason = f"arrives {-slack} min too late"
            elif slack < AT_RISK_SLACK_MIN:
                status, reason = "at_risk", f"only {slack} min of slack left"
            else:
                status, reason = "ok", None
            state[n.id] = (n.start, n.end, status == "broken")
            impacts[n.id] = NodeImpact(node_id=n.id, kind=n.kind, status=status, reason=reason)
        else:
            new_s = max(n.start, required)
            shift = int((new_s - n.start).total_seconds() // 60)
            new_e = n.end + timedelta(minutes=shift) if n.kind != "hotel" else n.end
            status, reason = ("shifted", f"starts {shift} min later") if shift > 0 else ("ok", None)
            if n.latest_start is not None and new_s > n.latest_start:
                status, reason = "at_risk", "arrival after hotel late check-in cutoff; confirm late arrival"
            state[n.id] = (new_s, new_e, False)
            impacts[n.id] = NodeImpact(node_id=n.id, kind=n.kind, status=status, shift_min=shift, reason=reason)

    # A cancelled/rescheduled-to-another-day return flight means the traveller may need more nights.
    for hotel in (n for n in trip.nodes if n.extension_dependents):
        for dep in hotel.extension_dependents:
            if dep == node_id and (cancelled or start.date() > target.start.date()):
                impacts[hotel.id] = NodeImpact(
                    node_id=hotel.id, kind=hotel.kind, status="at_risk",
                    reason=f"'{dep}' unavailable that day; stay may need extending",
                )  # fmt: skip
    return ImpactReport(disrupted=node_id, impacts=list(impacts.values()))
