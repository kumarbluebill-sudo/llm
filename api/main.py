from fastapi import FastAPI

from api.schemas import PlanRequest, PlanResponse

app = FastAPI(title="Travel LLM", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/plan", response_model=PlanResponse)
def plan(req: PlanRequest) -> PlanResponse:
    # v0.1 stub; the planner agent arrives in v0.2.
    return PlanResponse(status="stub", query=req.query)
