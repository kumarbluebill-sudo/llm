"""Side-effecting steps. Each mutating step goes through `idempotent`, which returns the stored
result if the same key already completed, and otherwise sends the key to the supplier."""
from collections.abc import Callable
from typing import Any

from saga.mock_providers import MockPayments, MockSupplier
from saga.store import BookingStore


def idempotent(store: BookingStore, key: str, fn: Callable[[str], Any]) -> Any:
    existing = store.get_idem(key)
    if existing and existing["status"] == "DONE":
        return existing["result"]
    store.put_idem(key, "IN_PROGRESS")
    result = fn(key)
    store.put_idem(key, "DONE", result)
    return result


class Activities:
    """Names here are the activity names used by the workflow."""

    def __init__(self, store: BookingStore, supplier: MockSupplier, payments: MockPayments):
        self.store, self.supplier, self.payments = store, supplier, payments

    def _idem(self, key: str, fn: Callable[[str], Any]) -> Any:
        return idempotent(self.store, key, fn)

    # --- bookkeeping -------------------------------------------------------
    def record(self, a: dict) -> dict:
        self.store.append(a["booking_id"], a["type"], a.get("state"), a.get("data"))
        return {}

    # --- forward steps -----------------------------------------------------
    def hold_flight(self, a: dict) -> dict:
        key = f"{a['booking_id']}:flight:{a['offer_id']}"
        return self._idem(key, lambda k: self.supplier.hold_flight(k, a["quoted"]))

    def hold_hotel(self, a: dict) -> dict:
        key = f"{a['booking_id']}:hotel:{a['hotel_id']}"
        return self._idem(key, lambda k: self.supplier.hold_hotel(k, a["quoted"]))

    def authorize_payment(self, a: dict) -> dict:
        key = f"{a['booking_id']}:payment"
        return self._idem(
            f"{key}:authorize", lambda _: self.payments.authorize(key, a["amount"], a["token"])
        )

    def confirm_hotel(self, a: dict) -> dict:
        return self._idem(
            f"{a['hold_key']}:confirm", lambda _: self.supplier.confirm_hotel(a["hold_key"])
        )

    def ticket_flight(self, a: dict) -> dict:
        return self._idem(
            f"{a['hold_key']}:ticket", lambda _: self.supplier.ticket_flight(a["hold_key"])
        )

    def capture_payment(self, a: dict) -> dict:
        key = f"{a['booking_id']}:payment"
        return self._idem(f"{key}:capture", lambda _: self.payments.capture(key))

    # --- compensations (safe to call when nothing happened) -----------------
    def cancel_flight(self, a: dict) -> dict:
        return self._idem(
            f"{a['hold_key']}:cancel", lambda _: self.supplier.cancel("cancel_flight", a["hold_key"])
        )

    def cancel_hotel(self, a: dict) -> dict:
        return self._idem(
            f"{a['hold_key']}:cancel", lambda _: self.supplier.cancel("cancel_hotel", a["hold_key"])
        )

    def refund_ticket(self, a: dict) -> dict:
        return self._idem(
            f"{a['hold_key']}:refund", lambda _: self.supplier.refund_ticket(a["hold_key"])
        )

    def void_payment(self, a: dict) -> dict:
        key = f"{a['booking_id']}:payment"
        return self._idem(f"{key}:void", lambda _: self.payments.void(key))


ACTIVITY_NAMES = [
    "record", "hold_flight", "hold_hotel", "authorize_payment", "confirm_hotel", "ticket_flight",
    "capture_payment", "cancel_flight", "cancel_hotel", "refund_ticket", "void_payment",
]  # fmt: skip
