"""Temporal worker. Env: TEMPORAL_HOST (default localhost:7233), POSTGRES_URL (required).
Uses mock supplier/payment providers until real credentials exist."""
import asyncio
import os

from temporalio.client import Client

from saga.activities import Activities
from saga.mock_providers import MockPayments, MockSupplier
from saga.store import PostgresStore
from saga.temporal_runtime import make_worker


async def main() -> None:
    client = await Client.connect(os.getenv("TEMPORAL_HOST", "localhost:7233"))
    acts = Activities(PostgresStore(os.environ["POSTGRES_URL"]), MockSupplier(), MockPayments())
    print("worker started")
    await make_worker(client, acts).run()


if __name__ == "__main__":
    asyncio.run(main())
