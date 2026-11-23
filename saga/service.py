"""Booking service used by the API. In-process by default; Temporal when TEMPORAL_HOST and
POSTGRES_URL are set (state is then shared through Postgres)."""
import asyncio
import os
from typing import Protocol

from saga.activities import Activities
from saga.direct import run_direct
from saga.mock_providers import MockPayments, MockSupplier
from saga.models import TERMINAL, BookingRequest
from saga.store import BookingStore, InMemoryStore, PostgresStore, latest_state


class BookingService(Protocol):
    store: BookingStore

    async def start(self, req: BookingRequest) -> None: ...
    async def approve(self, booking_id: str, decision: str) -> bool: ...


def status(store: BookingStore, booking_id: str) -> dict | None:
    events = store.events(booking_id)
    if not events:
        return None
    state = latest_state(events)
    done = state in {s.value for s in TERMINAL}
    if state == "TICKETED":
        # Money is captured after ticketing; the booking is not finished until then.
        done = any(e["type"] == "PAYMENT_CAPTURED" for e in events)
    return {"booking_id": booking_id, "state": state, "done": done, "events": events}


class InProcessService:
    def __init__(self, store: BookingStore | None = None) -> None:
        self.store = store or InMemoryStore()
        self.acts = Activities(self.store, MockSupplier(), MockPayments())
        self._approvals: dict[str, asyncio.Future[str]] = {}
        self._tasks: set[asyncio.Task] = set()

    async def start(self, req: BookingRequest) -> None:
        self.store.append(req.booking_id, "STATE", "DRAFT")  # visible to /booking immediately
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._approvals[req.booking_id] = fut
        task = asyncio.create_task(run_direct(req.model_dump(), self.acts, fut))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def approve(self, booking_id: str, decision: str) -> bool:
        fut = self._approvals.get(booking_id)
        if fut is None or fut.done():
            return False
        fut.set_result(decision)
        return True


class TemporalService:
    def __init__(self, host: str, store: BookingStore) -> None:
        self._host, self.store = host, store
        self._client = None

    async def _get_client(self):
        from temporalio.client import Client

        if self._client is None:
            self._client = await Client.connect(self._host)
        return self._client

    async def start(self, req: BookingRequest) -> None:
        from saga.workflow import TASK_QUEUE, TravelBookingWorkflow

        self.store.append(req.booking_id, "STATE", "DRAFT")
        client = await self._get_client()
        await client.start_workflow(
            TravelBookingWorkflow.run, req.model_dump(), id=req.booking_id, task_queue=TASK_QUEUE
        )

    async def approve(self, booking_id: str, decision: str) -> bool:
        from saga.workflow import TravelBookingWorkflow

        client = await self._get_client()
        handle = client.get_workflow_handle(booking_id)
        await handle.signal(TravelBookingWorkflow.approval, decision)
        return True


def build_service() -> BookingService:
    host, pg = os.getenv("TEMPORAL_HOST"), os.getenv("POSTGRES_URL")
    if host and pg:
        return TemporalService(host, PostgresStore(pg))
    return InProcessService()
