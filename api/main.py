import os
from functools import lru_cache

from fastapi import FastAPI, HTTPException

from agents.aggregator import aggregate
from agents.llm import AnthropicLLM, LLMClient, MockLLM
from agents.planner import PlannerError, plan
from agents.pricer import PricingError, price
from api.schemas import PlanRequest, PlanResponse
from mcp_servers.resilience import SupplierUnavailable
from mcp_servers.toolbox import Toolbox, mock_toolbox

app = FastAPI(title="Travel LLM", version="0.3.0")


def get_llm() -> LLMClient:
    # LLM_PROVIDER=anthropic switches to the real model; default is the mock.
    return AnthropicLLM() if os.getenv("LLM_PROVIDER") == "anthropic" else MockLLM()


@lru_cache(maxsize=1)
def get_toolbox() -> Toolbox:
    # Mock suppliers until real supplier credentials exist.
    return mock_toolbox()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/plan", response_model=PlanResponse)
def plan_trip(req: PlanRequest) -> PlanResponse:
    try:
        manifest = plan(req.query, get_llm())
    except PlannerError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if manifest.event is None:
        return PlanResponse(status="ok", query=req.query, manifest=manifest)
    try:
        itinerary = aggregate(manifest, price(manifest, get_toolbox()))
    except PricingError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except SupplierUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return PlanResponse(status="ok", query=req.query, manifest=manifest, itinerary=itinerary)
