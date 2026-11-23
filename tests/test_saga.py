import asyncio

import pytest

from saga.activities import Activities, idempotent
from saga.direct import run_direct
from saga.mock_providers import FaultInjector, MockPayments, MockSupplier
from saga.models import BookingRequest, FlightLeg, HotelStay
from saga.policy import check_policy
from saga.store import InMemoryStore, latest_state


def make_req(**kw) -> dict:
    base = {
        "booking_id": "b1",
        "flights": [FlightLeg(offer_id="DEL-DGH-1", quoted=6000), FlightLeg(offer_id="DGH-DEL-1", quoted=6500)],
        "hotel": HotelStay(hotel_id="dh-1", nights=2, quoted=7000),
        "payment_token": "tok_test",
    }
    base.update(kw)
    return BookingRequest(**base).model_dump()


class World:
    def __init__(self, drift=0.0):
        self.faults = FaultInjector()
        self.supplier = MockSupplier(self.faults, price_drift=drift)
        self.payments = MockPayments(self.faults)
        self.store = InMemoryStore()
        self.acts = Activities(self.store, self.supplier, self.payments)

    def run(self, req=None, approval=None):
        return asyncio.run(run_direct(req or make_req(), self.acts, approval))

    def states(self, bid="b1"):
        return [e["state"] for e in self.store.events(bid) if e["state"]]


def assert_clean(w: World):
    """After a cancelled booking nothing may remain live and no money may be captured."""
    assert w.supplier.live() == []
    assert all(a["status"] != "CAPTURED" for a in w.payments.auths.values())


def test_happy_path():
    w = World()
    res = w.run()
    assert res["status"] == "TICKETED" and res["total"] == 19500
    assert w.states() == ["QUOTED", "HELD", "AUTHORIZED", "CONFIRMED", "TICKETED"]
    statuses = {k: r["status"] for k, r in w.supplier.records.items()}
    assert sorted(statuses.values()) == ["CONFIRMED", "TICKETED", "TICKETED"]
    assert next(iter(w.payments.auths.values()))["status"] == "CAPTURED"
    assert any(e["type"] == "PAYMENT_CAPTURED" for e in w.store.events("b1"))


@pytest.mark.parametrize(
    "op",
    ["hold_flight", "hold_hotel", "authorize_payment", "confirm_hotel", "ticket_flight", "capture_payment"],
)
def test_failure_at_every_step_compensates(op):
    w = World()
    w.faults.inject(op, "reject")
    res = w.run()
    assert res["status"] == "CANCELLED"
    assert w.states()[-1] == "CANCELLED"
    assert_clean(w)


def test_second_ticket_failure_refunds_first_ticket():
    w = World()
    # first ticket succeeds, second is rejected
    calls = {"n": 0}
    orig = w.supplier.ticket_flight

    def flaky(key):
        calls["n"] += 1
        if calls["n"] == 2:
            from saga.errors import SupplierRejected

            raise SupplierRejected("sold out")
        return orig(key)

    w.supplier.ticket_flight = flaky
    res = w.run()
    assert res["status"] == "CANCELLED"
    assert "REFUNDED" in {r["status"] for r in w.supplier.records.values()}
    assert_clean(w)


def test_transient_failures_are_retried():
    w = World()
    w.faults.inject("hold_flight", "before", times=2)
    assert w.run()["status"] == "TICKETED"


@pytest.mark.parametrize("op", ["hold_flight", "hold_hotel", "ticket_flight", "confirm_hotel"])
def test_timeout_after_commit_never_duplicates(op):
    w = World()
    w.faults.inject(op, "after", times=1)
    assert w.run()["status"] == "TICKETED"
    assert all(n == 1 for n in w.supplier.created.values())  # 0 duplicate bookings


def test_authorize_timeout_after_commit_single_auth():
    w = World()
    w.faults.inject("authorize_payment", "after", times=1)
    assert w.run()["status"] == "TICKETED"
    assert len(w.payments.auths) == 1


def test_exhausted_retries_compensate_even_if_commit_happened():
    w = World()
    w.faults.inject("hold_hotel", "after", times=4)  # supplier keeps the hold, we never see success
    res = w.run()
    assert res["status"] == "CANCELLED"
    assert_clean(w)  # compensation registered before the step, so the orphan hold is released


def test_compensation_failure_needs_manual():
    w = World()
    w.faults.inject("authorize_payment", "reject")
    w.faults.inject("cancel_hotel", "reject")
    res = w.run()
    assert res["status"] == "NEEDS_MANUAL" and res["failed"]
    assert w.states()[-1] == "NEEDS_MANUAL"


def test_never_rebooks_forward_after_failure():
    w = World()
    w.faults.inject("authorize_payment", "reject")
    w.run()
    assert w.faults.calls["hold_flight"] == 2 and w.faults.calls["hold_hotel"] == 1


def test_state_is_persisted_before_each_external_call():
    w = World()
    w.run()
    types = [e["type"] for e in w.store.events("b1")]
    for i, t in enumerate(types):
        if t == "STEP_STARTED":
            assert types[i + 1] in {"STEP_STARTED", "STATE", "PAYMENT_CAPTURED"}
    assert types.count("STEP_STARTED") == 8


# --- human in the loop ----------------------------------------------------
def run_with_decision(w: World, decision: str | None, **req_kw):
    async def main():
        fut = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(run_direct(make_req(**req_kw), w.acts, fut))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if latest_state(w.store.events("b1")) == "AWAITING_APPROVAL":
                break
        assert latest_state(w.store.events("b1")) == "AWAITING_APPROVAL"
        assert w.payments.auths == {}  # no money authorized while waiting
        if decision:
            fut.set_result(decision)
        return await task

    return asyncio.run(main())


def test_price_drift_requires_approval_then_approved_completes():
    w = World(drift=0.05)
    assert run_with_decision(w, "approved")["status"] == "TICKETED"
    assert "APPROVED" in w.states()


def test_price_drift_rejected_releases_holds():
    w = World(drift=0.05)
    res = run_with_decision(w, "rejected")
    assert res["status"] == "CANCELLED"
    assert_clean(w)


def test_approval_timeout_cancels():
    w = World(drift=0.05)
    res = run_with_decision(w, None, approval_timeout_s=1)
    assert res["status"] == "CANCELLED" and "timed out" in res["reason"]
    assert_clean(w)


def test_high_value_requires_approval():
    w = World()
    res = run_with_decision(w, "rejected", hitl_total_inr=10_000)
    assert res["status"] == "CANCELLED"


def test_policy_reasons():
    req = make_req(acknowledged_nonrefundable=False)
    req["flights"][0]["refundable"] = False
    held_f = [{"price": 6000}, {"price": 6500}]
    reasons = check_policy(req, held_f, {"price": 7000})
    assert any("Non-refundable" in r for r in reasons)
    assert any("Conflicting" in r for r in reasons)
    assert check_policy(make_req(), held_f, {"price": 7000}) == []
    assert check_policy(make_req(), held_f, {"price": 7141}) != []  # just over 2%


# --- idempotency wrapper --------------------------------------------------
def test_idempotent_returns_stored_result_without_calling_supplier():
    store, calls = InMemoryStore(), []

    def fn(key):
        calls.append(key)
        return {"ok": 1}

    assert idempotent(store, "k", fn) == {"ok": 1}
    assert idempotent(store, "k", fn) == {"ok": 1}
    assert calls == ["k"]


def test_payment_receives_token_only():
    w = World()
    w.run()
    auth = next(iter(w.payments.auths.values()))
    assert auth["token"] == "tok_test" and "card" not in auth


def test_status_not_done_until_payment_captured():
    from saga.service import status

    store = InMemoryStore()
    store.append("x", "STATE", "TICKETED")
    assert status(store, "x")["done"] is False
    store.append("x", "PAYMENT_CAPTURED")
    assert status(store, "x")["done"] is True
    store.append("y", "STATE", "CANCELLED")
    assert status(store, "y")["done"] is True
