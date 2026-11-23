import json
import os
import secrets
import time
import uuid
from collections import OrderedDict
from collections.abc import Iterator
from datetime import UTC, datetime
from functools import lru_cache

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agents.aggregator import aggregate
from agents.llm import AnthropicLLM, LLMClient, MockLLM
from agents.planner import PlannerError, plan
from agents.pricer import PricingError, price
from api.config import load_env
from api.safety import MAX_QUERY_CHARS, looks_like_pan, redact_pan
from api.schemas import Itinerary, PlanRequest, PlanResponse
from disruption.manager import DisruptionManager
from disruption.models import DisruptionSignal, SignalType, Trip
from disruption.notify import RecordingSender
from mcp_servers.resilience import SupplierUnavailable
from mcp_servers.toolbox import Toolbox, build_toolbox
from saga.models import BookingRequest
from saga.reconcile import run_reconciliation
from saga.service import BookingService, build_service, status

load_env()  # local env files; real environment variables win
app = FastAPI(title="Travel LLM", version="0.8.0")
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
    # Mock flights/hotels until real supplier credentials exist; live activities if Viator is configured.
    return build_toolbox()


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


_TRIPS: dict[str, Trip] = {}
_BOOKING_BY_QUOTE: dict[str, str] = {}  # one quote can only ever be booked once


@lru_cache(maxsize=1)
def get_disruption_manager() -> DisruptionManager:
    svc = get_booking_service()
    acts = getattr(svc, "acts", None)
    if acts is None:
        raise HTTPException(status_code=501, detail="Disruption simulation needs in-process mode")
    # Mock traveller notifications; real push/SMS/WhatsApp/email senders come later.
    return DisruptionManager(_TRIPS, get_toolbox(), acts, RecordingSender())


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.get("/plan/stream")
def plan_stream(query: str = Query(min_length=3, max_length=MAX_QUERY_CHARS)) -> StreamingResponse:
    """Server-sent events: stage -> manifest -> stage -> itinerary -> done (or error)."""

    query = redact_pan(query)  # card numbers never reach the LLM

    def events() -> Iterator[str]:
        yield _sse("stage", {"stage": "planning"})
        try:
            manifest = plan(query, get_llm())
        except PlannerError:
            yield _sse("error", {"message": "Could not turn that into a valid trip request"})
            return
        except Exception:  # noqa: BLE001 - LLM provider/network failure
            yield _sse("error", {"message": "Planner temporarily unavailable"})
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
    safe_query = redact_pan(req.query)
    try:
        manifest = plan(safe_query, get_llm())
    except PlannerError as e:
        raise HTTPException(status_code=422, detail="Could not turn that into a valid trip request") from e
    except Exception as e:
        raise HTTPException(status_code=503, detail="Planner temporarily unavailable") from e
    if manifest.event is None:
        return PlanResponse(status="ok", query=safe_query, manifest=manifest)
    try:
        itinerary = aggregate(manifest, price(manifest, get_toolbox()))
    except PricingError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except SupplierUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    remember_quote(itinerary)
    return PlanResponse(status="ok", query=safe_query, manifest=manifest, itinerary=itinerary)


class BookRequest(BaseModel):
    itinerary_id: str
    payment_token: str = Field(min_length=3, max_length=200)  # a token from the payment provider, never a card
    acknowledged_nonrefundable: bool = False


class ApprovalRequest(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")


@app.post("/book", status_code=202)
async def book(req: BookRequest) -> dict:
    if looks_like_pan(req.payment_token):
        raise HTTPException(status_code=422, detail="Send a payment token, never a card number")
    existing = _BOOKING_BY_QUOTE.get(req.itinerary_id)
    if existing is not None:  # retry / double-click: never create a second purchase
        return {"booking_id": existing, "state": "QUOTED", "duplicate": True}
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
    _TRIPS[booking.booking_id] = Trip.from_itinerary(
        itinerary, booking.booking_id, payment_token=req.payment_token
    )
    _BOOKING_BY_QUOTE[req.itinerary_id] = booking.booking_id
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


class DisruptRequest(BaseModel):
    type: SignalType
    delay_minutes: int = Field(default=0, ge=0)
    notice_hours: float | None = None
    flight: str = Field(default="out", pattern="^(out|return)$")


class ChooseRequest(BaseModel):
    option_id: str


@app.post("/disrupt/{booking_id}")
def disrupt(booking_id: str, req: DisruptRequest) -> dict:
    """Inject a test disruption into a ticketed booking and run the full pipeline."""
    trip = _TRIPS.get(booking_id)
    st = status(get_booking_service().store, booking_id)
    if trip is None or st is None:
        raise HTTPException(status_code=404, detail="Unknown booking")
    if not st["done"] or st["state"] != "TICKETED":
        raise HTTPException(status_code=409, detail=f"Booking is {st['state']}; only ticketed bookings can be disrupted")
    node = trip.node("flight_out" if req.flight == "out" else "flight_return")
    now = datetime.now(UTC)
    sig = DisruptionSignal(
        signal_id=uuid.uuid4().hex, type=req.type, flight_ref=node.ref or "",
        delay_minutes=req.delay_minutes, notice_hours=req.notice_hours,
        source="test", occurred_at=now, received_at=now,
    )  # fmt: skip
    return get_disruption_manager().handle(sig).model_dump(mode="json")


@app.get("/disruption/{case_id}")
def disruption_case(case_id: str) -> dict:
    case = get_disruption_manager().cases.get(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Unknown case")
    return case.model_dump(mode="json")


@app.post("/disruption/{case_id}/choose")
def disruption_choose(case_id: str, req: ChooseRequest) -> dict:
    mgr = get_disruption_manager()
    if case_id not in mgr.cases:
        raise HTTPException(status_code=404, detail="Unknown case")
    try:
        return mgr.choose(case_id, req.option_id).model_dump(mode="json")
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Unknown option") from e
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@app.post("/disruption/{case_id}/dismiss")
def disruption_dismiss(case_id: str) -> dict:
    mgr = get_disruption_manager()
    if case_id not in mgr.cases:
        raise HTTPException(status_code=404, detail="Unknown case")
    return mgr.dismiss(case_id).model_dump(mode="json")


@app.post("/admin/reconcile")
def admin_reconcile(x_admin_token: str = Header(default="")) -> dict:
    """Run reconciliation now (a scheduler should call this nightly). Disabled unless ADMIN_TOKEN is set."""
    expected = os.getenv("ADMIN_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=404, detail="Not found")
    if not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=403, detail="Forbidden")
    acts = getattr(get_booking_service(), "acts", None)
    if acts is None:
        raise HTTPException(status_code=501, detail="Reconciliation needs supplier feeds; in-process mode only for now")
    rep = run_reconciliation(acts.store, acts.supplier.records, acts.payments.auths)
    return {"checked": rep.checked, "skipped_in_flight": rep.skipped_in_flight, "counts": rep.counts(),
            "findings": [f.model_dump() for f in rep.findings]}  # fmt: skip
