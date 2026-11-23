import os

from fastapi import FastAPI, HTTPException

from agents.llm import AnthropicLLM, LLMClient, MockLLM
from agents.planner import PlannerError, plan
from api.schemas import PlanRequest, PlanResponse

app = FastAPI(title="Travel LLM", version="0.2.0")


def get_llm() -> LLMClient:
    # LLM_PROVIDER=anthropic switches to the real model; default is the mock.
    return AnthropicLLM() if os.getenv("LLM_PROVIDER") == "anthropic" else MockLLM()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/plan", response_model=PlanResponse)
def plan_trip(req: PlanRequest) -> PlanResponse:
    try:
        manifest = plan(req.query, get_llm())
    except PlannerError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return PlanResponse(status="ok", query=req.query, manifest=manifest)
