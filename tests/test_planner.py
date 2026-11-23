import json
from pathlib import Path

import pytest

from agents.llm import MockLLM
from agents.planner import PlannerError, extract_json, plan

CASES = [
    json.loads(line)
    for line in Path("data/test_queries.jsonl").read_text(encoding="utf-8").splitlines()
]


def test_has_20_queries():
    assert len(CASES) == 20


@pytest.mark.parametrize("case", CASES, ids=[c["query"][:40] for c in CASES])
def test_mock_planner_valid_and_correct(case):
    m = plan(case["query"], MockLLM())  # raises if schema-invalid
    assert m.event and m.event.venue == case["venue"]
    assert m.budget.max_total == case["max_total"]
    assert m.travelers.adults == case["adults"]
    assert m.constraints.refundable_only is ("non-refundable ok" not in case["query"])


class Flaky:
    def __init__(self, outputs):
        self.outputs, self.calls = list(outputs), 0

    def complete(self, system, user):
        self.calls += 1
        return self.outputs.pop(0)


def test_retries_then_succeeds():
    good = MockLLM().complete("", "QUERY: RCB vs GT, Dharamshala, 2 people, 25k")
    llm = Flaky(["not json", '{"trip_id": "x"}', good])
    assert plan("RCB vs GT, Dharamshala", llm).event.venue == "Dharamshala"
    assert llm.calls == 3


def test_gives_up_after_max_attempts():
    with pytest.raises(PlannerError):
        plan("anything goes here", Flaky(["bad"] * 3))


def test_extract_json_handles_fences_and_prose():
    assert extract_json('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}


def test_facts_reach_the_prompt():
    seen = {}

    class Spy(MockLLM):
        def complete(self, system, user):
            seen["user"] = user
            return super().complete(system, user)

    plan("RCB vs GT, Dharamshala, 25k", Spy())
    assert "Gaggal" in seen["user"]
