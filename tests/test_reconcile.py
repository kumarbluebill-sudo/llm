from saga.reconcile import RECON_ID, reconcile, run_reconciliation
from tests.test_disruption import SignalType
from tests.test_disruption import World as DisruptionWorld
from tests.test_saga import World


def recon(w):
    return reconcile(w.store, w.supplier.records, w.payments.auths)


def kinds(rep):
    return sorted(f.type for f in rep.findings)


def test_clean_after_successful_booking():
    w = World()
    w.run()
    rep = recon(w)
    assert rep.clean and rep.checked == 1


def test_clean_after_every_compensated_failure():
    for op in ["hold_hotel", "authorize_payment", "confirm_hotel", "ticket_flight", "capture_payment"]:
        w = World()
        w.faults.inject(op, "reject")
        assert w.run()["status"] == "CANCELLED"
        assert recon(w).clean, op


def test_ghost_booking_when_supplier_lost_the_record():
    w = World()
    w.run()
    key = next(k for k in w.supplier.records if ":hotel:" in k)
    del w.supplier.records[key]
    rep = recon(w)
    assert kinds(rep) == ["GHOST_BOOKING"] and rep.findings[0].resource == key


def test_orphan_booking_unknown_to_us():
    w = World()
    w.run()
    w.supplier.records["someone-else:flight:X"] = {"kind": "flight", "status": "TICKETED", "price": 5000}
    rep = recon(w)
    assert kinds(rep) == ["ORPHAN_BOOKING"] and rep.findings[0].booking_id == "someone-else"


def test_cancelled_booking_with_live_supplier_inventory_is_orphan():
    w = World()
    w.faults.inject("authorize_payment", "reject")
    w.run()
    key = next(iter(w.supplier.records))
    w.supplier.records[key]["status"] = "HELD"  # supplier failed to honour the cancel
    assert kinds(recon(w)) == ["ORPHAN_BOOKING"]


def test_ticketed_without_capture_is_a_revenue_leak():
    w = World()
    w.run()
    next(iter(w.payments.auths.values()))["status"] = "AUTHORIZED"
    assert "PAYMENT_MISMATCH" in kinds(recon(w))


def test_amount_mismatch():
    w = World()
    w.run()
    next(iter(w.payments.auths.values()))["amount"] -= 100
    rep = recon(w)
    assert kinds(rep) == ["PAYMENT_MISMATCH"] and "amount" in rep.findings[0].detail


def test_cancelled_but_money_still_taken():
    w = World()
    w.faults.inject("ticket_flight", "reject")
    w.run()
    next(iter(w.payments.auths.values()))["status"] = "CAPTURED"
    assert "PAYMENT_MISMATCH" in kinds(recon(w))


def test_status_mismatch():
    w = World()
    w.run()
    key = next(k for k in w.supplier.records if ":flight:" in k)
    w.supplier.records[key]["status"] = "CANCELLED"
    assert kinds(recon(w)) == ["STATUS_MISMATCH"]


def test_orphan_payment_with_no_booking():
    w = World()
    w.run()
    w.payments.auths["ghost:payment"] = {"status": "CAPTURED", "amount": 10, "token": "t"}
    assert kinds(recon(w)) == ["PAYMENT_MISMATCH"]


def test_needs_manual_is_reported():
    w = World()
    w.faults.inject("authorize_payment", "reject")
    w.faults.inject("cancel_hotel", "reject")
    assert w.run()["status"] == "NEEDS_MANUAL"
    assert "OPEN_MANUAL" in kinds(recon(w))


def test_in_flight_bookings_are_skipped_not_flagged():
    w = World()
    w.store.append("b9", "STATE", "QUOTED", {"resources": ["b9:flight:X"]})
    w.store.append("b9", "STATE", "HELD")
    w.supplier.records["b9:flight:X"] = {"kind": "flight", "status": "HELD", "price": 1}
    rep = recon(w)
    assert rep.clean and rep.skipped_in_flight == 1


def test_findings_become_persisted_tickets():
    w = World()
    w.run()
    del w.supplier.records[next(iter(w.supplier.records))]
    rep = run_reconciliation(w.store, w.supplier.records, w.payments.auths)
    tickets = [e for e in w.store.events(RECON_ID) if e["type"] == "RECON_FINDING"]
    assert len(tickets) == len(rep.findings) == 1
    # the reconciliation pseudo-booking itself is never reconciled
    assert reconcile(w.store, w.supplier.records, w.payments.auths).checked == 1


# --- with disruption rebooking -------------------------------------------------
def test_clean_after_disruption_rebooking_and_delta_payment():
    w = DisruptionWorld(same_carrier=True)
    case = w.mgr.handle(w.signal())
    assert case.status == "RESOLVED"
    rep = reconcile(w.store, w.supplier.records, w.payments.auths)
    assert rep.clean, rep.findings


def test_clean_after_chosen_option_and_released_holds():
    w = DisruptionWorld()
    case = w.mgr.handle(w.signal())
    w.mgr.choose(case.case_id, case.options[0].option_id)
    assert reconcile(w.store, w.supplier.records, w.payments.auths).clean


def test_open_disruption_holds_are_not_orphans_but_dismissed_leftovers_are():
    w = DisruptionWorld()
    case = w.mgr.handle(w.signal(SignalType.CANCELLATION))
    assert case.status == "AWAITING_APPROVAL"
    assert reconcile(w.store, w.supplier.records, w.payments.auths).clean  # holds still valid
    w.mgr.dismiss(case.case_id)
    leftover = case.options[0].hold_id
    w.supplier.records[leftover]["status"] = "HELD"  # release silently failed
    rep = reconcile(w.store, w.supplier.records, w.payments.auths)
    assert kinds(rep) == ["ORPHAN_BOOKING"] and rep.findings[0].resource == leftover
