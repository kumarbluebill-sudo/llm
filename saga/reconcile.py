"""Nightly reconciliation (doc section 8.3): match internal booking records against supplier
confirmation feeds and payment settlements.

  GHOST_BOOKING     internal says booked, supplier has nothing   (revenue-bearing promise we cannot honour)
  ORPHAN_BOOKING    supplier holds live inventory we do not know (silent cost leak)
  PAYMENT_MISMATCH  money and ticket disagree (ticketed but not captured, captured but cancelled, ...)
  STATUS_MISMATCH   both sides know the booking but disagree on its status
  OPEN_MANUAL       saga ended in NEEDS_MANUAL and still needs a human
"""
from typing import Literal

from pydantic import BaseModel

from saga.store import BookingStore, latest_state

LIVE = {"HELD", "CONFIRMED", "TICKETED"}
RECON_ID = "__reconciliation__"

FindingType = Literal["GHOST_BOOKING", "ORPHAN_BOOKING", "PAYMENT_MISMATCH", "STATUS_MISMATCH", "OPEN_MANUAL"]


class Finding(BaseModel):
    type: FindingType
    booking_id: str
    detail: str
    resource: str | None = None


class Report(BaseModel):
    checked: int = 0
    skipped_in_flight: int = 0
    findings: list[Finding] = []

    @property
    def clean(self) -> bool:
        return not self.findings

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.type] = out.get(f.type, 0) + 1
        return out


def _bid(key: str) -> str:
    return key.split(":")[0]


def _kind(key: str) -> str:
    return "hotel" if ":hotel:" in key else "flight"


def _case_states(events: list[dict]) -> dict[str, str]:
    """Last known step per disruption case."""
    out: dict[str, str] = {}
    for e in events:
        if e["type"] == "DISRUPTION":
            out[e["data"]["case"]] = e["data"]["step"]
    return out


def reconcile(store: BookingStore, supplier: dict[str, dict], payments: dict[str, dict]) -> Report:
    rep = Report()
    known: set[str] = set()
    expected_live: set[str] = set()
    owned_payments: set[str] = set()
    closed_case_prefixes: set[str] = set()

    for bid in store.booking_ids():
        if bid == RECON_ID:
            continue
        events = store.events(bid)
        known.add(bid)
        state = latest_state(events)
        quoted = next((e for e in events if e["state"] == "QUOTED"), None)
        resources = list(quoted["data"].get("resources", [])) if quoted else []
        captured = next((e for e in events if e["type"] == "PAYMENT_CAPTURED"), None)
        cases = _case_states(events)
        replaced: dict[str, str] = {}
        for e in events:
            if e["type"] == "DISRUPTION" and e["data"]["step"] == "RESOLVED" and e["data"].get("new_key"):
                replaced[e["data"]["replaced_key"]] = e["data"]["new_key"]
        for cid, step in cases.items():
            if step in {"DISMISSED", "RESOLVED", "EXEC_FAILED"}:
                closed_case_prefixes.add(f"{bid}:disr-{cid}:")

        pay_key = f"{bid}:payment"
        owned_payments.add(pay_key)
        owned_payments.update(k for k in payments if k.startswith(f"{bid}:delta:"))

        if state in {"TICKETED", "CANCELLED", "NEEDS_MANUAL"} or state is None:
            pass
        else:
            rep.skipped_in_flight += 1
            expected_live.update(resources)
            continue
        rep.checked += 1

        if state == "TICKETED":
            if captured is None:
                rep.findings.append(Finding(type="PAYMENT_MISMATCH", booking_id=bid, resource=pay_key,
                                            detail="booking is ticketed but payment capture never completed"))  # fmt: skip
            for key in resources:
                if key in replaced:  # rebooked by a disruption case
                    old = supplier.get(key)
                    if old and old["status"] in LIVE:
                        rep.findings.append(Finding(type="STATUS_MISMATCH", booking_id=bid, resource=key,
                                                    detail=f"replaced flight still {old['status']} at supplier"))  # fmt: skip
                    key = replaced[key]
                expected_live.add(key)
                want = "CONFIRMED" if _kind(key) == "hotel" else "TICKETED"
                rec = supplier.get(key)
                if rec is None:
                    rep.findings.append(Finding(type="GHOST_BOOKING", booking_id=bid, resource=key,
                                                detail="internal record says booked; supplier has no such booking"))  # fmt: skip
                elif rec["status"] != want:
                    rep.findings.append(Finding(type="STATUS_MISMATCH", booking_id=bid, resource=key,
                                                detail=f"expected {want}, supplier says {rec['status']}"))  # fmt: skip
            pay = payments.get(pay_key)
            amount = captured["data"].get("amount") if captured else None
            if captured is not None and (pay is None or pay["status"] != "CAPTURED"):
                rep.findings.append(Finding(type="PAYMENT_MISMATCH", booking_id=bid, resource=pay_key,
                                            detail="internal says captured; payment processor disagrees"))  # fmt: skip
            elif pay is not None and amount is not None and pay["amount"] != amount:
                rep.findings.append(Finding(type="PAYMENT_MISMATCH", booking_id=bid, resource=pay_key,
                                            detail=f"amount {amount} internally vs {pay['amount']} at processor"))  # fmt: skip
        elif state == "CANCELLED":
            for key in resources:
                rec = supplier.get(key)
                if rec and rec["status"] in LIVE:
                    rep.findings.append(Finding(type="ORPHAN_BOOKING", booking_id=bid, resource=key,
                                                detail=f"booking cancelled but supplier still {rec['status']}"))  # fmt: skip
            pay = payments.get(pay_key)
            if pay and pay["status"] in {"CAPTURED", "AUTHORIZED"}:
                rep.findings.append(Finding(type="PAYMENT_MISMATCH", booking_id=bid, resource=pay_key,
                                            detail=f"booking cancelled but payment is {pay['status']}"))  # fmt: skip
        elif state == "NEEDS_MANUAL":
            expected_live.update(resources)
            rep.findings.append(Finding(type="OPEN_MANUAL", booking_id=bid,
                                        detail="saga ended in NEEDS_MANUAL; resolve compensation by hand"))  # fmt: skip

    flagged = {f.resource for f in rep.findings if f.resource}
    for key, rec in supplier.items():
        if rec["status"] not in LIVE or key in expected_live or key in flagged:
            continue
        bid = _bid(key)
        if bid not in known:
            rep.findings.append(Finding(type="ORPHAN_BOOKING", booking_id=bid, resource=key,
                                        detail="supplier holds live inventory with no internal booking"))  # fmt: skip
        elif ":disr-" in key and not any(key.startswith(p) for p in closed_case_prefixes):
            continue  # hold belongs to a still-open disruption case
        elif key not in expected_live:
            rep.findings.append(Finding(type="ORPHAN_BOOKING", booking_id=bid, resource=key,
                                        detail="live supplier inventory not tied to any open booking or case"))  # fmt: skip

    for key, pay in payments.items():
        if pay["status"] == "CAPTURED" and key not in owned_payments:
            rep.findings.append(Finding(type="PAYMENT_MISMATCH", booking_id=_bid(key), resource=key,
                                        detail="captured payment with no internal booking"))  # fmt: skip
    return rep


def run_reconciliation(store: BookingStore, supplier: dict[str, dict], payments: dict[str, dict]) -> Report:
    """Reconcile and turn every mismatch into a persisted ticket event."""
    rep = reconcile(store, supplier, payments)
    for f in rep.findings:
        store.append(RECON_ID, "RECON_FINDING", None, f.model_dump())
    return rep
