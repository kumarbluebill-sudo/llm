import json
import os
import time
import uuid
from collections import OrderedDict
from collections.abc import Iterator
from functools import lru_cache

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agents.aggregator import aggregate
from agents.llm import AnthropicLLM, LLMClient, MockLLM
from agents.planner import PlannerError, plan
from agents.pricer import PricingError, price
from api.schemas import Itinerary, PlanRequest, PlanResponse
from mcp_servers.resilience import SupplierUnavailable
from mcp_servers.toolbox import Toolbox, mock_toolbox
from saga.models import BookingRequest
from saga.service import BookingService, build_service, status

app = FastAPI(title="Travel LLM", version="0.5.0")
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


QUOTE_TTL_S = 30 * 60  # doc: QUOTED state lives 15-30 min
_QUOTES: OrderedDict[str, tuple[float, Itinerary]] = OrderedDict()


def remember_quote(it: Itinerary) -> None:
    """The server keeps the quote; /book never trusts prices sent by the client."""
    _QUOTES[it.itinerary_id] = (time.monotonic(), it)
    while len(_QUOTES) > 500:
        _QUOTES.popitem(last=False)


@lru_cache(maxsize=1)
def get_booking_service() -> BookingService:
    return build_service()


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
            remember_quote(itinerary)
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
    remember_quote(itinerary)
    return PlanResponse(status="ok", query=req.query, manifest=manifest, itinerary=itinerary)


class BookRequest(BaseModel):
    itinerary_id: str
    payment_token: str = Field(min_length=3)  # a token from the payment provider, never a card
    acknowledged_nonrefundable: bool = False


class ApprovalRequest(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")


@app.post("/book", status_code=202)
async def book(req: BookRequest) -> dict:
    quote = _QUOTES.get(req.itinerary_id)
    if quote is None or time.monotonic() - quote[0] > QUOTE_TTL_S:
        raise HTTPException(status_code=410, detail="Quote expired or unknown; plan again")
    itinerary = quote[1]
    if not itinerary.within_budget:
        raise HTTPException(status_code=422, detail="Itinerary exceeds the budget; adjust the plan")
    booking = BookingRequest.from_itinerary(
        itinerary,
        req.payment_token,
        booking_id=str(uuid.uuid4()),
        acknowledged_nonrefundable=req.acknowledged_nonrefundable,
    )
    await get_booking_service().start(booking)
    return {"booking_id": booking.booking_id, "state": "QUOTED"}


@app.get("/booking/{booking_id}")
def booking_status(booking_id: str) -> dict:
    st = status(get_booking_service().store, booking_id)
    if st is None:
        raise HTTPException(status_code=404, detail="Unknown booking")
    return st


@app.post("/booking/{booking_id}/approval")
async def booking_approval(booking_id: str, req: ApprovalRequest) -> dict:
    st = status(get_booking_service().store, booking_id)
    if st is None:
        raise HTTPException(status_code=404, detail="Unknown booking")
    if st["state"] != "AWAITING_APPROVAL":
        raise HTTPException(status_code=409, detail=f"Booking is {st['state']}, not awaiting approval")
    await get_booking_service().approve(booking_id, req.decision)
    return {"booking_id": booking_id, "decision": req.decision}
