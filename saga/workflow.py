"""Temporal binding for the saga. All decisions live in saga.core; this only supplies durable
activity calls and the human-approval signal."""
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from saga.core import run_booking

TASK_QUEUE = "travel-booking"

_RETRY = RetryPolicy(
    maximum_attempts=4,
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    non_retryable_error_types=["SupplierRejected"],
)


class TemporalSteps:
    def __init__(self, wf: "TravelBookingWorkflow") -> None:
        self._wf = wf

    async def run(self, name: str, arg: dict) -> dict:
        return await workflow.execute_activity(
            name, arg, start_to_close_timeout=timedelta(seconds=30), retry_policy=_RETRY
        )

    async def wait_approval(self, timeout_s: int) -> str | None:
        try:
            await workflow.wait_condition(
                lambda: self._wf.decision is not None, timeout=timedelta(seconds=timeout_s)
            )
        except TimeoutError:
            return None
        return self._wf.decision


@workflow.defn
class TravelBookingWorkflow:
    def __init__(self) -> None:
        self.decision: str | None = None

    @workflow.signal
    def approval(self, decision: str) -> None:
        self.decision = decision

    @workflow.run
    async def run(self, req: dict) -> dict:
        return await run_booking(req, TemporalSteps(self))
