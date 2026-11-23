"""Mock supplier + payment gateway with fault injection. State-based, so repeating a call
with the same key never creates a second booking (as a real idempotent supplier behaves)."""
import threading

from saga.errors import SupplierRejected, TransientError


class FaultInjector:
    """inject(op, kind): 'before' = fail before applying, 'after' = apply then time out,
    'reject' = business rejection. Consumed one per call."""

    def __init__(self) -> None:
        self._faults: dict[str, list[str]] = {}
        self.calls: dict[str, int] = {}
        self._lock = threading.Lock()

    def inject(self, op: str, kind: str = "reject", times: int = 1) -> None:
        self._faults.setdefault(op, []).extend([kind] * times)

    def enter(self, op: str) -> str | None:
        with self._lock:
            self.calls[op] = self.calls.get(op, 0) + 1
            queue = self._faults.get(op)
            kind = queue.pop(0) if queue else None
        if kind == "reject":
            raise SupplierRejected(f"{op} rejected")
        if kind == "before":
            raise TransientError(f"{op} timed out before applying")
        return kind  # 'after' is handled by the caller once state is applied

    @staticmethod
    def leave(op: str, kind: str | None) -> None:
        if kind == "after":
            raise TransientError(f"{op} timed out after applying")


class MockSupplier:
    """Flights + hotels. `records` maps resource key -> {kind, status, price}."""

    def __init__(self, faults: FaultInjector | None = None, price_drift: float = 0.0) -> None:
        self.faults = faults or FaultInjector()
        self.price_drift = price_drift
        self.records: dict[str, dict] = {}
        self.created: dict[str, int] = {}

    def _hold(self, op: str, kind: str, key: str, quoted: int) -> dict:
        fk = self.faults.enter(op)
        if key not in self.records:
            price = round(quoted * (1 + self.price_drift))
            self.records[key] = {"kind": kind, "status": "HELD", "price": price}
            self.created[key] = self.created.get(key, 0) + 1
        self.faults.leave(op, fk)
        return {"hold_id": key, "price": self.records[key]["price"]}

    def hold_flight(self, key: str, quoted: int) -> dict:
        return self._hold("hold_flight", "flight", key, quoted)

    def hold_hotel(self, key: str, quoted: int) -> dict:
        return self._hold("hold_hotel", "hotel", key, quoted)

    def _move(self, op: str, key: str, allowed: set[str], new: str) -> dict:
        fk = self.faults.enter(op)
        rec = self.records.get(key)
        if rec is not None and rec["status"] in allowed:
            rec["status"] = new
        self.faults.leave(op, fk)
        return {"key": key, "status": rec["status"] if rec else "NOT_FOUND"}

    def ticket_flight(self, key: str) -> dict:
        return self._move("ticket_flight", key, {"HELD"}, "TICKETED")

    def confirm_hotel(self, key: str) -> dict:
        return self._move("confirm_hotel", key, {"HELD"}, "CONFIRMED")

    def cancel(self, op: str, key: str) -> dict:  # cancel_flight / cancel_hotel
        return self._move(op, key, {"HELD", "CONFIRMED"}, "CANCELLED")

    def refund_ticket(self, key: str) -> dict:
        return self._move("refund_ticket", key, {"TICKETED"}, "REFUNDED")

    def live(self) -> list[str]:
        return [k for k, r in self.records.items() if r["status"] in {"HELD", "CONFIRMED", "TICKETED"}]


class MockPayments:
    """Razorpay-shaped authorize -> capture / void. Receives a token, never card data."""

    def __init__(self, faults: FaultInjector | None = None) -> None:
        self.faults = faults or FaultInjector()
        self.auths: dict[str, dict] = {}

    def authorize(self, key: str, amount: int, token: str) -> dict:
        fk = self.faults.enter("authorize_payment")
        self.auths.setdefault(key, {"status": "AUTHORIZED", "amount": amount, "token": token})
        self.faults.leave("authorize_payment", fk)
        return {"auth_id": key, "amount": self.auths[key]["amount"]}

    def capture(self, key: str) -> dict:
        fk = self.faults.enter("capture_payment")
        a = self.auths.get(key)
        if a is None:
            raise SupplierRejected("nothing authorized to capture")
        if a["status"] == "AUTHORIZED":
            a["status"] = "CAPTURED"
        self.faults.leave("capture_payment", fk)
        return {"auth_id": key, "status": a["status"]}

    def void(self, key: str) -> dict:
        fk = self.faults.enter("void_payment")
        a = self.auths.get(key)
        if a is not None:
            if a["status"] == "CAPTURED":
                raise SupplierRejected("cannot void a captured payment")
            a["status"] = "VOIDED"
        self.faults.leave("void_payment", fk)
        return {"auth_id": key, "status": a["status"] if a else "NOT_FOUND"}
