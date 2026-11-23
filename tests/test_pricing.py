import json
from datetime import date
from pathlib import Path

import pytest

from agents.aggregator import aggregate
from agents.budget import quote_tiers
from agents.llm import MockLLM
from agents.planner import plan
from agents.pricer import PricingError, price
from api.schemas import Manifest
from mcp_servers.mock_suppliers import MockFlights, MockHotels, MockTransport, MockWeather
from mcp_servers.resilience import Guarded
from mcp_servers.toolbox import Toolbox, mock_toolbox

CASES = [
    json.loads(line)
    for line in Path("data/test_queries.jsonl").read_text(encoding="utf-8").splitlines()
]


def manifest(max_total=60000, tier="mid", refundable=True, adults=2, venue="Dharamshala"):
    return Manifest.model_validate(
        {
            "trip_id": "t",
            "event": {"name": "RCB vs GT", "venue": venue, "date": date(2027, 4, 15).isoformat()},
            "travelers": {"adults": adults},
            "budget": {"tier": tier, "max_total": max_total},
            "constraints": {"refundable_only": refundable},
        }
    )


def test_fare_rules_catch_listing_trap():
    opts = price(manifest(), mock_toolbox())
    assert all(o.offer_id.endswith(("-2", "-3")) for o in opts.outbound + opts.inbound)
    assert any("non-refundable fare rules" in n for n in opts.notes)


def test_allowing_non_refundable_widens_options():
    strict = price(manifest(refundable=True), mock_toolbox())
    loose = price(manifest(refundable=False), mock_toolbox())
    assert len(loose.outbound) > len(strict.outbound)


def test_tier_totals_are_ordered():
    m = manifest()
    q = quote_tiers(m, price(m, mock_toolbox()))
    assert q["budget"].total <= q["mid"].total <= q["premium"].total


def test_steps_down_when_over_budget():
    m = manifest(tier="premium")
    opts = price(m, mock_toolbox())
    q = quote_tiers(m, opts)
    m2 = manifest(tier="premium", max_total=q["mid"].total)
    it = aggregate(m2, price(m2, mock_toolbox()))
    assert it.tier == "mid" and it.within_budget
    assert any("exceeds budget" in w for w in it.warnings)


def test_flags_when_nothing_fits():
    m = manifest(max_total=1000)
    it = aggregate(m, price(m, mock_toolbox()))
    assert it.tier == "budget" and not it.within_budget
    assert it.total_cost == it.tier_totals["budget"]


def test_itinerary_segments_and_actions():
    m = manifest()
    it = aggregate(m, price(m, mock_toolbox()))
    assert [s.type for s in it.segments] == ["flight", "hotel", "transfer", "event", "transfer", "flight"]
    assert it.total_cost == sum(s.cost for s in it.segments)
    assert {a.action for a in it.booking_actions} == {"commit_saga", "partial_commit"}


def test_no_destination_raises():
    m = manifest()
    m.event = None
    with pytest.raises(PricingError):
        price(m, mock_toolbox())


def test_flaky_supplier_recovers_via_retry():
    tools = Toolbox(
        Guarded(MockFlights(fail_times=2), sleep=lambda _: None),
        Guarded(MockHotels()),
        Guarded(MockTransport()),
        Guarded(MockWeather()),
    )
    assert price(manifest(), tools).outbound


@pytest.mark.parametrize("case", CASES, ids=[c["query"][:40] for c in CASES])
def test_end_to_end_all_queries(case):
    m = plan(case["query"], MockLLM())
    it = aggregate(m, price(m, mock_toolbox()))
    assert it.total_cost > 0
    assert it.total_cost == sum(s.cost for s in it.segments)
    assert all(s.refundable is not False for s in it.segments if m.constraints.refundable_only)
