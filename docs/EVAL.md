# Evaluation (v0.7)

Run: `python -m evals.run` (mock planner) or `python -m evals.run --provider anthropic` (needs `ANTHROPIC_API_KEY`).
Results are written to `evals/results/latest.json`; regression floors live in `evals/baseline.json`.

## What is measured
| Metric | Meaning | Doc target |
|---|---|---|
| json_validity | first planner output already parses and validates | 100% |
| schema_compliance | a valid Manifest was produced (after retries) | - |
| intent_* | venue, budget, travellers, refundable constraint match the query's ground truth | - |
| budget_consistency | itinerary total equals its segments AND the `within_budget` flag is truthful | >= 98% |
| plans_within_budget | share of plans that actually fit the user's budget (depends on mock price levels) | - |
| grounding | itinerary segments whose price/ids/refundability match what supplier tools return | >= 89% |
| redteam_pass | 45 adversarial checks (injection, abuse, PII, API misuse) | >= 53% |

## Latest run (mock planner, 320 synthetic cases, 45 red-team checks)
json_validity 100, schema_compliance 100, intent_exact 87.8 (venue 96.6, budget 93.1, travellers 95.6, refundable 100),
budget_consistency 100, plans_within_budget 41.7, grounding 100, redteam_pass 100.

## How to read these numbers
- **Planner metrics measure the mock, not Claude.** The mock is a regex heuristic that we wrote; json_validity 100% is
  guaranteed by construction. The 39 intent failures are the deliberately "hard" cases (word-numbers like "twenty five
  thousand", "two of us", city aliases like Bangalore). Real-model numbers do not exist yet.
- **The golden set is synthetic**, generated from templates. It does not represent real user phrasing.
- **Grounding 100% is partly tautological** (the aggregator is built from the same tools the check queries), but it does
  catch arithmetic/id regressions (see `test_grounding_check_catches_tampering`).
- **plans_within_budget 41.7%** is driven by arbitrary mock price levels versus the golden budgets, not by planner quality.
  The system's guarantee is honesty (budget_consistency), not that every request is affordable.
- **Red-team injection cases cannot fool the mock**, so they only become informative with a real LLM. The API/PII
  invariants (card redaction, quote-once booking, ignored client prices, admin lockout) are tested for real.
- The doc's ROUGE-L target does not apply: there is no reference text to compare against.

## Not yet done
- Real-Claude run of the golden set and red-team prompts.
- Evaluation of disruption quality beyond unit tests; human review of the golden set.
