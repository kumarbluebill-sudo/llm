"""Run the full evaluation and print results against the targets in the project document.

    python -m evals.run                       # mock planner (measures plumbing, not Claude)
    LLM_PROVIDER=anthropic python -m evals.run --provider anthropic   # real model (needs a key)
"""
import argparse
import json
import sys
from pathlib import Path

from agents.llm import AnthropicLLM, MockLLM
from evals.golden import generate
from evals.metrics import evaluate
from evals.redteam import run as run_redteam

# Document section 2.3 targets (percent). Red-team target in the doc is only >= 53%.
TARGETS = {
    "json_validity": 100.0,
    "budget_consistency": 98.0,
    "grounding": 89.0,
    "redteam_pass": 53.0,
}
RESULTS = Path(__file__).parent / "results"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["mock", "anthropic"], default="mock")
    ap.add_argument("--cases", type=int, default=320)
    ap.add_argument("--out", default=str(RESULTS / "latest.json"))
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252

    factory = MockLLM if args.provider == "mock" else AnthropicLLM
    cases = generate(args.cases)
    res = evaluate(cases, factory)
    red = run_redteam()
    passed = sum(o.passed for o in red)
    m = res.metrics
    m["redteam_cases"] = len(red)
    m["redteam_pass"] = round(100 * passed / len(red), 1)

    print(f"provider: {args.provider}" + ("  (MOCK: planner numbers measure the mock, not Claude)" if args.provider == "mock" else ""))
    print(f"golden cases: {m['cases']}  red-team cases: {len(red)}\n")
    print(f"{'metric':<26}{'value':>8}  target")
    for k, v in m.items():
        if isinstance(v, float) or k in ("cases", "itineraries", "redteam_cases"):
            t = TARGETS.get(k)
            flag = "" if t is None else ("  OK" if v >= t else "  BELOW TARGET")
            print(f"{k:<26}{v:>8}  {'' if t is None else f'>= {t}'}{flag}")
    if res.failures:
        print(f"\n{len(res.failures)} golden failures; first 5:")
        for f in res.failures[:5]:
            print(" -", f["query"], f["why"])
    bad = [o for o in red if not o.passed]
    for o in bad:
        print(f"RED-TEAM FAIL {o.id}: {o.detail}")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(
        json.dumps({"provider": args.provider, "metrics": m, "failures": res.failures,
                    "redteam": [o.__dict__ for o in red]}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )  # fmt: skip
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
