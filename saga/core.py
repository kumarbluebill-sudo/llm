"""Booking saga logic, independent of the runtime. `Steps` is implemented by a Temporal adapter
(durable) and by an in-process adapter (dev/tests). Must stay deterministic: no clocks, no I/O.

Rules enforced here (doc section 5 / 8):
- every compensation is registered BEFORE its forward step runs (a step that times out after
  committing can still be undone; compensations are no-ops when nothing happened)
- state is persisted before every external call
- holds first, then irreversible steps; payment capture is last
- on failure we compensate backwards and NEVER rebook forward on our own
"""
from typing import Protocol

from saga.errors import BookingAborted
from saga.policy import check_policy


class Steps(Protocol):
    async def run(self, name: str, arg: dict) -> dict: ...
    async def wait_approval(self, timeout_s: int) -> str | None: ...


async def run_booking(req: dict, steps: Steps) -> dict:
    bid = req["booking_id"]
    compensations: list[tuple[str, dict]] = []

    async def state(name: str, **data) -> None:
        await steps.run("record", {"booking_id": bid, "type": "STATE", "state": name, "data": data})

    async def step(name: str, arg: dict) -> dict:
        await steps.run("record", {"booking_id": bid, "type": "STEP_STARTED", "data": {"step": name}})
        return await steps.run(name, {"booking_id": bid, **arg})

    try:
        resources = [f"{bid}:flight:{f['offer_id']}" for f in req["flights"]]
        resources.append(f"{bid}:hotel:{req['hotel']['hotel_id']}")
        await state(
            "QUOTED",
            total_quoted=sum(f["quoted"] for f in req["flights"]) + req["hotel"]["quoted"],
            resources=resources,
        )

        # 1. HOLD everything (cheap to undo)
        held_flights: list[dict] = []
        for leg in req["flights"]:
            key = f"{bid}:flight:{leg['offer_id']}"
            compensations.append(("cancel_flight", {"booking_id": bid, "hold_key": key}))
            held_flights.append(await step("hold_flight", {"offer_id": leg["offer_id"], "quoted": leg["quoted"]}))
        hotel = req["hotel"]
        hotel_key = f"{bid}:hotel:{hotel['hotel_id']}"
        compensations.append(("cancel_hotel", {"booking_id": bid, "hold_key": hotel_key}))
        held_hotel = await step("hold_hotel", {"hotel_id": hotel["hotel_id"], "quoted": hotel["quoted"]})
        total_held = sum(h["price"] for h in held_flights) + held_hotel["price"]
        await state("HELD", total_held=total_held)

        # 2. Human-in-the-loop gate
        reasons = check_policy(req, held_flights, held_hotel)
        if reasons:
            await state("AWAITING_APPROVAL", reasons=reasons)
            decision = await steps.wait_approval(req.get("approval_timeout_s", 86400))
            if decision != "approved":
                raise BookingAborted(f"approval {decision or 'timed out'}: {'; '.join(reasons)}")
            await state("APPROVED")

        # 3. Authorize (not capture) payment
        compensations.append(("void_payment", {"booking_id": bid}))
        await step("authorize_payment", {"amount": total_held, "token": req["payment_token"]})
        await state("AUTHORIZED", amount=total_held)

        # 4. Commit in ascending cancellation-penalty order: refundable hotel, then tickets
        await step("confirm_hotel", {"hold_key": hotel_key})
        await state("CONFIRMED")
        for leg in req["flights"]:
            key = f"{bid}:flight:{leg['offer_id']}"
            compensations.append(("refund_ticket", {"booking_id": bid, "hold_key": key}))
            await step("ticket_flight", {"hold_key": key})
        await state("TICKETED")

        # 5. Capture only after everything is confirmed
        await step("capture_payment", {})
        await steps.run(
            "record", {"booking_id": bid, "type": "PAYMENT_CAPTURED", "data": {"amount": total_held}}
        )
        return {"booking_id": bid, "status": "TICKETED", "total": total_held}

    except Exception as e:  # noqa: BLE001 - any failure triggers compensation
        reason = str(e)
        await state("COMPENSATING", reason=reason)
        failed: list[str] = []
        for name, arg in reversed(compensations):
            try:
                await steps.run(name, arg)
            except Exception as ce:  # noqa: BLE001
                failed.append(f"{name}: {ce}")
        if failed:
            await state("NEEDS_MANUAL", reason=reason, failed_compensations=failed)
            return {"booking_id": bid, "status": "NEEDS_MANUAL", "reason": reason, "failed": failed}
        await state("CANCELLED", reason=reason)
        return {"booking_id": bid, "status": "CANCELLED", "reason": reason}
