"""OPTIONS: rank 3-5 rebooking candidates, check each against the trip graph, and hold inventory."""
from collections.abc import Callable
from datetime import date, datetime, time, timedelta

from pydantic import BaseModel

from disruption.autonomy import AutonomyPolicy, decide
from disruption.impact import analyze
from disruption.models import Trip, TripNode
from mcp_servers.models import FlightOffer
from mcp_servers.toolbox import Toolbox

HOLD_MINUTES = 10


class RebookOption(BaseModel):
    option_id: str
    offer: FlightOffer
    depart_at: datetime
    arrive_at: datetime
    cost_delta: int  # INR for all passengers; negative = cheaper
    same_carrier: bool
    same_day: bool
    refundable: bool
    feasible: bool
    notes: list[str] = []
    decision: str = ""
    decision_reasons: list[str] = []
    hold_id: str | None = None
    hold_expires_at: datetime | None = None


def parse_offer_id(offer_id: str) -> tuple[str, str, date]:
    origin, dest, y, m, d, _ = offer_id.split("-")
    return origin, dest, date(int(y), int(m), int(d))


def _times(offer: FlightOffer) -> tuple[datetime, datetime]:
    h, m = map(int, offer.depart.split(":"))
    start = datetime.combine(offer.day, time(h, m))
    return start, start + timedelta(minutes=offer.duration_min)


def build_options(
    trip: Trip,
    node: TripNode,
    toolbox: Toolbox,
    policy: AutonomyPolicy,
    hold_fn: Callable[[RebookOption], dict | None],
    now: datetime,
    *,
    k: int = 5,
) -> list[RebookOption]:
    origin, dest, day = parse_offer_id(node.ref or "")
    candidates: list[FlightOffer] = []
    for d in (day, day + timedelta(days=1)):
        candidates += toolbox.search_flights(origin, dest, d, False)

    options: list[RebookOption] = []
    for c in candidates:
        if c.offer_id == node.ref:
            continue
        start, end = _times(c)
        report = analyze(trip, node.id, new_start=start, new_end=end)
        refundable = toolbox.get_fare_rules(c.offer_id).refundable
        same_day = c.day == day
        same_carrier = c.carrier == node.carrier
        delta = (c.price - node.price) * trip.pax
        decision, reasons = decide(
            cost_delta=delta,
            same_carrier=same_carrier,
            same_day=same_day,
            downgrade=trip.refundable_booked and not refundable,
            policy=policy,
        )
        options.append(
            RebookOption(
                option_id=c.offer_id,
                offer=c,
                depart_at=start,
                arrive_at=end,
                cost_delta=delta,
                same_carrier=same_carrier,
                same_day=same_day,
                refundable=refundable,
                feasible=report.feasible,
                notes=[f"{i.node_id}: {i.reason}" for i in report.impacts if i.status != "ok" and i.reason],
                decision=decision.value,
                decision_reasons=reasons,
            )
        )

    options.sort(key=lambda o: (not o.feasible, not o.refundable, not o.same_carrier, o.cost_delta, o.arrive_at))
    held: list[RebookOption] = []
    for o in options:
        if len(held) == k:
            break
        if not o.feasible:
            continue
        hold = hold_fn(o)
        if hold is None:
            continue  # inventory gone: never offer something we cannot hold
        o.hold_id = hold["hold_id"]
        o.hold_expires_at = now + timedelta(minutes=HOLD_MINUTES)
        held.append(o)
    return held
