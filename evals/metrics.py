"""Measures the planner and the planning pipeline against the golden set."""
import math
from collections.abc import Callable
from dataclasses import dataclass, field

from agents.aggregator import aggregate
from agents.knowledge import CITIES
from agents.llm import LLMClient
from agents.planner import PlannerError, extract_json, plan
from agents.pricer import PricingError, price
from api.schemas import Itinerary, Manifest
from evals.golden import GoldenCase
from mcp_servers.resilience import SupplierUnavailable
from mcp_servers.toolbox import Toolbox, mock_toolbox


class RecordingLLM:
    """Wraps any LLMClient and remembers whether the FIRST raw output was already valid."""

    def __init__(self, inner: LLMClient) -> None:
        self.inner = inner
        self.first_valid: bool | None = None

    def complete(self, system: str, user: str) -> str:
        raw = self.inner.complete(system, user)
        if self.first_valid is None:
            try:
                Manifest.model_validate(extract_json(raw))
                self.first_valid = True
            except Exception:  # noqa: BLE001
                self.first_valid = False
        return raw


@dataclass
class Result:
    metrics: dict[str, float] = field(default_factory=dict)
    failures: list[dict] = field(default_factory=list)


def check_grounding(it: Itinerary, m: Manifest, tools: Toolbox) -> tuple[int, int]:
    """Count itinerary segments whose facts match what the supplier tools actually return."""
    assert m.event is not None
    pax = m.travelers.adults + m.travelers.children
    rooms = math.ceil(pax / 2)
    grounded = total = 0
    for s in it.segments:
        total += 1
        d = s.details
        ok = False
        if s.type == "flight":
            from datetime import date

            offers = tools.search_flights(d["origin"], d["dest"], date.fromisoformat(d["day"]), False)
            hit = next((o for o in offers if o.offer_id == d["offer_id"]), None)
            ok = hit is not None and s.cost == hit.price * pax
            if ok and s.refundable:
                ok = tools.get_fare_rules(d["offer_id"]).refundable  # authoritative fare rule
        elif s.type == "hotel":
            hit = next((h for h in tools.search_hotels(m.event.venue, False) if h.hotel_id == d["hotel_id"]), None)
            ok = hit is not None and s.cost == hit.nightly_price * d["nights"] * rooms
        elif s.type == "transfer":
            r = tools.get_route(d["airport"], d["venue"])
            ok = r.minutes == d["minutes"] and r.cost_inr == d["cost_inr"]
        elif s.type == "event":
            ok = d["venue"] == m.event.venue and d["name"] == m.event.name
        grounded += ok
    return grounded, total


def evaluate(cases: list[GoldenCase], llm_factory: Callable[[], LLMClient]) -> Result:
    n = len(cases)
    first_valid = schema_ok = 0
    field_hits = {"venue": 0, "budget": 0, "travelers": 0, "refundable": 0}
    exact = 0
    grounded_venue = 0
    itineraries = fits = consistent = 0
    seg_ok = seg_total = 0
    failures: list[dict] = []
    tools = mock_toolbox()

    for c in cases:
        llm = RecordingLLM(llm_factory())
        try:
            m = plan(c.query, llm)
        except PlannerError:
            failures.append({"id": c.id, "query": c.query, "why": "no valid manifest"})
            continue
        schema_ok += 1
        first_valid += bool(llm.first_valid)
        v = bool(m.event and m.event.venue == c.venue)
        b = m.budget.max_total == c.max_total
        t = m.travelers.adults == c.adults
        r = m.constraints.refundable_only == c.refundable_only
        for k, ok in (("venue", v), ("budget", b), ("travelers", t), ("refundable", r)):
            field_hits[k] += ok
        exact += v and b and t and r
        grounded_venue += bool(m.event and m.event.venue in CITIES)
        if not (v and b and t and r):
            failures.append({"id": c.id, "query": c.query, "category": c.category,
                             "why": {"venue": v, "budget": b, "travelers": t, "refundable": r}})  # fmt: skip
        if m.event is None:
            continue
        try:
            it = aggregate(m, price(m, tools))
        except (PricingError, SupplierUnavailable):
            continue
        itineraries += 1
        fits += it.within_budget
        consistent += it.total_cost == sum(s.cost for s in it.segments) and it.within_budget == (
            it.total_cost <= m.budget.max_total
        )
        g, tot = check_grounding(it, m, tools)
        seg_ok, seg_total = seg_ok + g, seg_total + tot

    pct = lambda a, b: round(100 * a / b, 1) if b else 0.0
    metrics = {
        "cases": n,
        "json_validity": pct(first_valid, n),
        "schema_compliance": pct(schema_ok, n),
        "intent_exact": pct(exact, n),
        "intent_venue": pct(field_hits["venue"], n),
        "intent_budget": pct(field_hits["budget"], n),
        "intent_travelers": pct(field_hits["travelers"], n),
        "intent_refundable": pct(field_hits["refundable"], n),
        "planner_venue_grounded": pct(grounded_venue, schema_ok),
        "plans_within_budget": pct(fits, itineraries),
        "budget_consistency": pct(consistent, itineraries),
        "grounding": pct(seg_ok, seg_total),
        "itineraries": itineraries,
    }
    return Result(metrics, failures)
