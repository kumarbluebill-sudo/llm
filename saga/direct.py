"""In-process runtime for the saga: used for dev, tests and the default API (no Temporal needed).
Mirrors Temporal's behaviour: transient errors are retried, rejections are not."""
import asyncio

from api.telemetry import span
from saga.activities import Activities
from saga.core import run_booking
from saga.errors import TransientError

MAX_ATTEMPTS = 4


class DirectSteps:
    def __init__(self, activities: Activities, approval: asyncio.Future[str] | None = None):
        self._acts = activities
        self._approval = approval

    async def run(self, name: str, arg: dict) -> dict:
        fn = getattr(self._acts, name)
        with span("saga.step", step=name, booking_id=arg.get("booking_id")) as sp:
            for attempt in range(MAX_ATTEMPTS):
                try:
                    result = fn(arg)
                except TransientError:
                    if attempt == MAX_ATTEMPTS - 1:
                        raise
                    continue
                sp.set_attribute("attempts", attempt + 1)
                return result
        raise AssertionError("unreachable")

    async def wait_approval(self, timeout_s: int) -> str | None:
        if self._approval is None:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(self._approval), timeout_s)
        except TimeoutError:
            return None


async def run_direct(req: dict, activities: Activities, approval: asyncio.Future[str] | None = None) -> dict:
    return await run_booking(req, DirectSteps(activities, approval))
