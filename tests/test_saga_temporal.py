"""Runs the real workflow on a Temporal test server (downloaded on first use).
Skipped automatically if the test server cannot be started."""
import asyncio
import uuid

import pytest
from temporalio.testing import WorkflowEnvironment

from saga.activities import Activities
from saga.mock_providers import FaultInjector, MockPayments, MockSupplier
from saga.models import BookingRequest, FlightLeg, HotelStay
from saga.store import InMemoryStore, latest_state
from saga.temporal_runtime import make_worker
from saga.workflow import TASK_QUEUE, TravelBookingWorkflow


def make_req(**kw) -> dict:
    base = {
        "booking_id": f"b-{uuid.uuid4().hex[:8]}",
        "flights": [FlightLeg(offer_id="A-1", quoted=6000), FlightLeg(offer_id="B-1", quoted=6500)],
        "hotel": HotelStay(hotel_id="h-1", nights=2, quoted=7000),
        "payment_token": "tok_test",
    }
    base.update(kw)
    return BookingRequest(**base).model_dump()


async def scenario(body, drift=0.0):
    try:
        env = await WorkflowEnvironment.start_time_skipping()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Temporal test server unavailable: {e}")
    faults = FaultInjector()
    supplier, payments, store = MockSupplier(faults, drift), MockPayments(faults), InMemoryStore()
    acts = Activities(store, supplier, payments)
    try:
        async with make_worker(env.client, acts):
            await body(env, faults, supplier, payments, store)
    finally:
        await env.shutdown()


def run(body, drift=0.0):
    asyncio.run(scenario(body, drift))


async def start(env, req):
    return await env.client.start_workflow(
        TravelBookingWorkflow.run, req, id=req["booking_id"], task_queue=TASK_QUEUE
    )


def test_happy_path_on_temporal():
    async def body(env, faults, supplier, payments, store):
        req = make_req()
        res = await (await start(env, req)).result()
        assert res["status"] == "TICKETED"
        assert latest_state(store.events(req["booking_id"])) == "TICKETED"
        assert all(n == 1 for n in supplier.created.values())

    run(body)


def test_rejection_compensates_on_temporal():
    async def body(env, faults, supplier, payments, store):
        faults.inject("ticket_flight", "reject")
        res = await (await start(env, make_req())).result()
        assert res["status"] == "CANCELLED"
        assert supplier.live() == []

    run(body)


def test_transient_errors_retried_by_temporal_no_duplicates():
    async def body(env, faults, supplier, payments, store):
        faults.inject("hold_flight", "after", times=2)
        res = await (await start(env, make_req())).result()
        assert res["status"] == "TICKETED"
        assert all(n == 1 for n in supplier.created.values())

    run(body)


def test_approval_signal_resumes_workflow():
    async def body(env, faults, supplier, payments, store):
        req = make_req()
        handle = await start(env, req)
        for _ in range(50):
            await asyncio.sleep(0.1)
            if latest_state(store.events(req["booking_id"])) == "AWAITING_APPROVAL":
                break
        assert latest_state(store.events(req["booking_id"])) == "AWAITING_APPROVAL"
        await handle.signal(TravelBookingWorkflow.approval, "approved")
        assert (await handle.result())["status"] == "TICKETED"

    run(body, drift=0.05)


def test_approval_timeout_cancels_durably():
    async def body(env, faults, supplier, payments, store):
        res = await (await start(env, make_req(approval_timeout_s=3600))).result()
        assert res["status"] == "CANCELLED" and "timed out" in res["reason"]
        assert supplier.live() == []

    run(body, drift=0.05)
