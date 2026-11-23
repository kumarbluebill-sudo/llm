"""In-process runtime for the saga: used for dev, tests and the default API (no Temporal needed).
Mirrors Temporal's behaviour: transient errors are retried, rejections are not."""
import asyncio

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
        for attempt in range(MAX_ATTEMPTS):
            try:
                return fn(arg)
            except TransientError:
                if attempt == MAX_ATTEMPTS - 1:
                    raise
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
