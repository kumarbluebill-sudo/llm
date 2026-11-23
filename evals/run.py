"""Run the full evaluation and print results against the targets in the project document.

    python -m evals.run                       # mock planner (measures plumbing, not Claude)
    LLM_PROVIDER=anthropic python -m evals.run --provider anthropic   # real model (needs a key)
"""
import argparse
import json
import os
import sys
from pathlib import Path

from agents.llm import AnthropicLLM, MockLLM, OpenAICompatLLM
from api.config import load_env
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


def force_offline_suppliers() -> None:
    """Evals must be deterministic and must never spend live-API quota (SerpApi/Viator)."""
    os.environ["SERPAPI_LIVE"] = "0"
    os.environ["VIATOR_LIVE"] = "0"
    os.environ["MAPBOX_LIVE"] = "0"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["mock", "anthropic", "freellm"], default="mock")
    ap.add_argument("--sample", type=int, default=0, help="evaluate an evenly spaced sample of N golden cases")
    ap.add_argument("--skip-redteam", action="store_true")
    ap.add_argument("--cases", type=int, default=320)
    ap.add_argument("--out", default=str(RESULTS / "latest.json"))
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252

    load_env()
    force_offline_suppliers()
    os.environ["LLM_PROVIDER"] = args.provider  # the red-team suite drives the real API with this model
    factory = {"mock": MockLLM, "anthropic": AnthropicLLM, "freellm": OpenAICompatLLM.from_env}[args.provider]
    cases = generate(args.cases)
    if args.sample:
        step = max(1, len(cases) // args.sample)
        cases = cases[::step][: args.sample]
    res = evaluate(cases, factory)
    red = [] if args.skip_redteam else run_redteam()
    passed = sum(o.passed is True for o in red)
    failed_n = sum(o.passed is False for o in red)
    m = res.metrics
    m["redteam_cases"] = len(red)
    m["redteam_inconclusive"] = sum(o.passed is None for o in red)
    m["redteam_pass"] = round(100 * passed / (passed + failed_n), 1) if (passed + failed_n) else 0.0

    print(f"provider: {args.provider}" + ("  (MOCK: planner numbers measure the mock, not a real model)" if args.provider == "mock" else ""))
    print(f"golden cases: {m['cases']}  red-team cases: {len(red)}\n")
    print(f"{'metric':<26}{'value':>8}  target")
    for k, v in m.items():
        if isinstance(v, float) or k in ("cases", "itineraries", "redteam_cases", "llm_unavailable", "redteam_inconclusive"):
            t = TARGETS.get(k)
            flag = "" if t is None else ("  OK" if v >= t else "  BELOW TARGET")
            print(f"{k:<26}{v:>8}  {'' if t is None else f'>= {t}'}{flag}")
    if res.failures:
        print(f"\n{len(res.failures)} golden failures; first 5:")
        for f in res.failures[:5]:
            print(" -", f["query"], f["why"])
    bad = [o for o in red if o.passed is False]
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
