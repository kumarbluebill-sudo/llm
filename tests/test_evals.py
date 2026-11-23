import json
from pathlib import Path

import pytest

from agents.aggregator import aggregate
from agents.llm import MockLLM
from agents.planner import plan
from agents.pricer import price
from evals.golden import generate
from evals.metrics import check_grounding, evaluate
from evals.redteam import PROMPT_CASES, run
from mcp_servers.toolbox import mock_toolbox

BASELINE = json.loads((Path("evals") / "baseline.json").read_text())


@pytest.fixture(scope="module")
def result():
    return evaluate(generate(), MockLLM)


def test_golden_set_is_large_unique_and_deterministic():
    a, b = generate(), generate()
    assert len(a) >= 300 and len({c.query for c in a}) == len(a)
    assert [c.query for c in a] == [c.query for c in b]
    assert {c.venue for c in a} >= {"Dharamshala", "Mumbai", "Bengaluru", "Delhi", "Lucknow"}
    hard = sum(c.category == "hard" for c in a)
    assert 0.08 < hard / len(a) < 0.4  # deliberately includes cases a weak planner will miss
    assert any(not c.refundable_only for c in a) and any(c.tier for c in a)


def test_no_regression_against_baseline(result):
    for k, floor in BASELINE.items():
        if k.startswith("_") or k == "redteam_pass":
            continue
        assert result.metrics[k] >= floor - 0.1, f"{k}: {result.metrics[k]} < baseline {floor}"


def test_document_targets_for_deterministic_parts(result):
    assert result.metrics["budget_consistency"] >= 98
    assert result.metrics["grounding"] >= 89
    assert result.metrics["json_validity"] == 100


def test_eval_is_sensitive_to_a_bad_planner():
    cases = generate(60)

    class Garbage:
        def complete(self, system, user):
            return "I cannot help with that."

    class IgnoresBudget(MockLLM):
        def complete(self, system, user):
            return super().complete(system, user).replace('"max_total": ', '"max_total": 1')

    assert evaluate(cases, Garbage).metrics["schema_compliance"] == 0
    assert evaluate(cases, IgnoresBudget).metrics["intent_budget"] < 10


def test_grounding_check_catches_tampering():
    tools = mock_toolbox()
    m = plan("RCB vs GT, Dharamshala, 2 people, 150k", MockLLM())
    it = aggregate(m, price(m, tools))
    g, total = check_grounding(it, m, tools)
    assert g == total
    it.segments[0].cost += 1  # a flight price the supplier never quoted
    it.segments[1].details["hotel_id"] = "invented-hotel"
    g2, _ = check_grounding(it, m, tools)
    assert g2 == total - 2


def test_red_team_all_pass():
    out = run()
    assert len(run()) == len(out)  # re-runnable: cases are not consumed
    assert len(out) == 45 and len(PROMPT_CASES) + 16 == 45
    failed = [(o.id, o.detail) for o in out if not o.passed]
    assert not failed, failed
