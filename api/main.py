import json
import os
from collections.abc import Iterator
from functools import lru_cache

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from agents.aggregator import aggregate
from agents.llm import AnthropicLLM, LLMClient, MockLLM
from agents.planner import PlannerError, plan
from agents.pricer import PricingError, price
from api.schemas import PlanRequest, PlanResponse
from mcp_servers.resilience import SupplierUnavailable
from mcp_servers.toolbox import Toolbox, mock_toolbox

app = FastAPI(title="Travel LLM", version="0.4.0")
app.add_middleware(
    CORSMiddleware,
    # Comma-separated origins, e.g. the Netlify site. Local dev uses the Vite proxy.
    allow_origins=[o for o in os.getenv("CORS_ORIGINS", "").split(",") if o],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def get_llm() -> LLMClient:
    # LLM_PROVIDER=anthropic switches to the real model; default is the mock.
    return AnthropicLLM() if os.getenv("LLM_PROVIDER") == "anthropic" else MockLLM()


@lru_cache(maxsize=1)
def get_toolbox() -> Toolbox:
    # Mock suppliers until real supplier credentials exist.
    return mock_toolbox()


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.get("/plan/stream")
def plan_stream(query: str = Query(min_length=3)) -> StreamingResponse:
    """Server-sent events: stage -> manifest -> stage -> itinerary -> done (or error)."""

    def events() -> Iterator[str]:
        yield _sse("stage", {"stage": "planning"})
        try:
            manifest = plan(query, get_llm())
        except PlannerError as e:
            yield _sse("error", {"message": str(e)})
            return
        yield _sse("manifest", manifest.model_dump(mode="json"))
        if manifest.event is not None:
            yield _sse("stage", {"stage": "pricing"})
            try:
                itinerary = aggregate(manifest, price(manifest, get_toolbox()))
            except (PricingError, SupplierUnavailable) as e:
                yield _sse("error", {"message": str(e)})
                return
            yield _sse("itinerary", itinerary.model_dump(mode="json"))
        yield _sse("done", {})

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )


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
