from concurrent.futures import ThreadPoolExecutor

from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from saga.activities import ACTIVITY_NAMES, Activities
from saga.errors import SupplierRejected
from saga.workflow import TASK_QUEUE, TravelBookingWorkflow


def _make_activity(name: str, fn):
    @activity.defn(name=name)
    def act(arg: dict) -> dict:
        try:
            return fn(arg)
        except SupplierRejected as e:
            raise ApplicationError(str(e), type="SupplierRejected", non_retryable=True) from e

    return act


def temporal_activities(acts: Activities) -> list:
    return [_make_activity(n, getattr(acts, n)) for n in ACTIVITY_NAMES]


def make_worker(client: Client, acts: Activities) -> Worker:
    return Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[TravelBookingWorkflow],
        activities=temporal_activities(acts),
        activity_executor=ThreadPoolExecutor(max_workers=8),
    )
