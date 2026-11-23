import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from agents.aggregator import aggregate
from agents.pricer import price
from api.schemas import Manifest
from disruption import detector
from disruption.autonomy import AutonomyPolicy, Decision, decide
from disruption.entitlements import FlightContext, draft_claim, entitlements
from disruption.impact import analyze, descendants
from disruption.manager import DisruptionManager
from disruption.models import DisruptionSignal, Severity, SignalType, Trip
from disruption.notify import RecordingSender, notify
from disruption.triage import triage
from mcp_servers.mock_suppliers import MockFlights, MockHotels, MockTransport, MockWeather
from mcp_servers.resilience import Guarded
from mcp_servers.toolbox import Toolbox
from saga.activities import Activities
from saga.direct import run_direct
from saga.mock_providers import FaultInjector, MockPayments, MockSupplier
from saga.models import BookingRequest
from saga.store import InMemoryStore

NOW = datetime(2027, 4, 10, 8, 0, tzinfo=UTC)


class SameCarrierFlights(MockFlights):
    def search_flights(self, *a, **kw):
        return [o.model_copy(update={"carrier": "IndiGo"}) for o in super().search_flights(*a, **kw)]


def toolbox(same_carrier: bool) -> Toolbox:
    flights = SameCarrierFlights() if same_carrier else MockFlights()
    return Toolbox(Guarded(flights), Guarded(MockHotels()), Guarded(MockTransport()), Guarded(MockWeather()))


class World:
    def __init__(self, same_carrier=False, tier="budget", sender=None, channels=None):
        self.faults = FaultInjector()
        self.supplier = MockSupplier(self.faults)
        self.payments = MockPayments(self.faults)
        self.store = InMemoryStore()
        self.acts = Activities(self.store, self.supplier, self.payments)
        self.tools = toolbox(same_carrier)
        manifest = Manifest.model_validate(
            {
                "trip_id": "t",
                "event": {"name": "RCB vs GT", "venue": "Dharamshala", "date": "2027-04-15"},
                "travelers": {"adults": 2},
                "budget": {"tier": tier, "max_total": 200000},
            }
        )
        self.itinerary = aggregate(manifest, price(manifest, self.tools))
        booking = BookingRequest.from_itinerary(self.itinerary, "tok_test", booking_id="bk1")
        assert asyncio.run(run_direct(booking.model_dump(), self.acts))["status"] == "TICKETED"
        self.trip = Trip.from_itinerary(self.itinerary, "bk1", payment_token="tok_test")
        if channels:
            self.trip.consent_channels = channels
        self.sender = sender or RecordingSender()
        self.clock = [NOW]
        self.mgr = DisruptionManager(
            {"bk1": self.trip}, self.tools, self.acts, self.sender, clock=lambda: self.clock[0]
        )

    @property
    def out_ref(self) -> str:
        return self.trip.node("flight_out").ref

    def signal(self, type_=SignalType.CANCELLATION, delay=0, notice=2.0, ref=None, flight_node="flight_out"):
        return DisruptionSignal(
            signal_id="s1",
            type=type_,
            flight_ref=ref or self.trip.node(flight_node).ref,
            delay_minutes=delay,
            notice_hours=notice,
            occurred_at=NOW - timedelta(seconds=20),
            received_at=NOW,
        )


# --- triage ----------------------------------------------------------------
@pytest.mark.parametrize(
    ("type_", "delay", "notice", "expected"),
    [
        (SignalType.DELAY, 30, None, Severity.LOW),
        (SignalType.DELAY, 59, None, Severity.LOW),
        (SignalType.DELAY, 60, None, Severity.MEDIUM),
        (SignalType.DELAY, 180, None, Severity.MEDIUM),
        (SignalType.DELAY, 181, None, Severity.HIGH),
        (SignalType.MISSED_CONNECTION, 0, None, Severity.HIGH),
        (SignalType.CANCELLATION, 0, 3, Severity.CRITICAL),
        (SignalType.CANCELLATION, 0, 23.9, Severity.CRITICAL),
        (SignalType.CANCELLATION, 0, 48, Severity.HIGH),
        (SignalType.DENIED_BOARDING, 0, None, Severity.CRITICAL),
    ],
)
def test_triage_rules(type_, delay, notice, expected):
    sig = DisruptionSignal(
        signal_id="x", type=type_, flight_ref="r", delay_minutes=delay, notice_hours=notice, occurred_at=NOW
    )
    assert triage(sig) is expected


# --- impact graph ------------------------------------------------------------
def test_small_delay_shifts_transfer_but_nothing_breaks():
    w = World()
    r = analyze(w.trip, "flight_out", delay_min=30)
    assert r.feasible and r.get("transfer_in").status == "shifted"


def test_cancelled_outbound_breaks_whole_downstream_chain():
    w = World()
    r = analyze(w.trip, "flight_out", cancelled=True)
    assert {"transfer_in", "hotel", "event"} <= set(r.broken)
    # the return transfer/flight depend on the event, so they must be flagged too
    assert {"transfer_out", "flight_return"} <= set(r.broken)


def test_late_arrival_flags_hotel_checkin():
    w = World()
    r = analyze(w.trip, "flight_out", delay_min=60 * 14)
    assert r.get("hotel").status in {"at_risk", "broken"}


def test_cancelled_return_flags_hotel_extension():
    w = World()
    r = analyze(w.trip, "flight_return", cancelled=True)
    assert r.get("hotel").status == "at_risk" and "extend" in r.get("hotel").reason


def test_delay_eats_event_slack():
    w = World()
    t = w.trip
    t.event_buffer_min = 60
    fo = t.node("flight_out")
    day = t.node("event").start.replace(hour=13, minute=0)
    fo.start, fo.end = day, day + timedelta(minutes=90)
    ti = t.node("transfer_in")
    dur = ti.end - ti.start
    ti.start = fo.end + timedelta(minutes=45)
    ti.end = ti.start + dur
    t.node("hotel").start = ti.end
    assert analyze(t, "flight_out", delay_min=0).get("event").status in {"ok", "at_risk"}
    assert analyze(t, "flight_out", delay_min=600).get("event").status == "broken"


@pytest.mark.parametrize("node_id", ["flight_out", "transfer_in", "hotel", "event", "transfer_out", "flight_return"])
@pytest.mark.parametrize("delay", [0, 20, 90, 240, 900])
@pytest.mark.parametrize("cancelled", [False, True])
def test_no_cascade_miss_every_dependent_is_evaluated(node_id, delay, cancelled):
    w = World()
    r = analyze(w.trip, node_id, delay_min=delay, cancelled=cancelled)
    evaluated = {i.node_id for i in r.impacts}
    assert descendants(w.trip, node_id) <= evaluated


# --- options --------------------------------------------------------------------
def test_cancellation_yields_held_ranked_options():
    w = World()
    case = w.mgr.handle(w.signal())
    assert case.severity is Severity.CRITICAL
    assert 1 <= len(case.options) <= 5
    for o in case.options:
        assert o.hold_id and o.hold_expires_at == NOW + timedelta(minutes=10)
        assert o.option_id != w.out_ref and o.feasible
    assert case.metrics["time_to_first_option_s"] < 90
    assert case.metrics["time_to_detection_s"] == 20


def test_options_are_real_holds_in_supplier():
    w = World()
    case = w.mgr.handle(w.signal())
    held = {k for k, r in w.supplier.records.items() if r["status"] == "HELD"}
    assert {o.hold_id for o in case.options} <= held


def test_cross_carrier_requires_approval_and_nothing_is_booked_yet():
    w = World(same_carrier=False)
    before = {k: r["status"] for k, r in w.supplier.records.items()}
    case = w.mgr.handle(w.signal())
    assert case.status == "AWAITING_APPROVAL"
    assert all(o.decision == Decision.REQUIRE_APPROVAL for o in case.options)
    assert all(before[k] == w.supplier.records[k]["status"] for k in before)  # original untouched


def test_choose_executes_after_approval():
    w = World()
    original = w.out_ref  # the trip graph is updated after execution
    case = w.mgr.handle(w.signal())
    opt = case.options[0]
    done = w.mgr.choose(case.case_id, opt.option_id)
    assert done.status == "RESOLVED" and done.chosen_option == opt.option_id
    assert w.supplier.records[opt.hold_id]["status"] == "TICKETED"
    assert w.supplier.records[f"bk1:flight:{original}"]["status"] == "REFUNDED"
    # every other held option was released
    assert all(
        w.supplier.records[o.hold_id]["status"] == "CANCELLED" for o in case.options if o.option_id != opt.option_id
    )
    assert w.trip.node("flight_out").ref == opt.option_id  # trip graph follows the new flight


def test_dismiss_releases_all_holds():
    w = World()
    case = w.mgr.handle(w.signal())
    w.mgr.dismiss(case.case_id)
    assert case.status == "DISMISSED"
    live = set(w.supplier.live())
    assert not any(o.hold_id in live for o in case.options)


def test_same_carrier_cheap_option_auto_executes_with_notice():
    w = World(same_carrier=True)
    case = w.mgr.handle(w.signal())
    assert case.options[0].decision == Decision.AUTO_EXECUTE_NOTIFY
    assert case.status == "RESOLVED" and case.chosen_option == case.options[0].option_id
    delta_auth = w.payments.auths.get(f"bk1:delta:{case.case_id}")
    assert delta_auth and delta_auth["status"] == "CAPTURED"
    assert delta_auth["amount"] == case.options[0].cost_delta


def test_denied_boarding_never_auto_executes():
    w = World(same_carrier=True)
    case = w.mgr.handle(w.signal(SignalType.DENIED_BOARDING, notice=None))
    assert case.status == "AWAITING_APPROVAL"


def test_failed_ticketing_never_improvises_and_releases_holds():
    w = World()
    case = w.mgr.handle(w.signal())
    w.faults.inject("ticket_flight", "reject")
    res = w.mgr.choose(case.case_id, case.options[0].option_id)
    assert res.status == "EXEC_FAILED"
    assert w.supplier.records[f"bk1:flight:{w.out_ref}"]["status"] == "TICKETED"  # original kept
    assert not any(o.hold_id in w.supplier.live() for o in case.options)


def test_low_delay_notifies_only_no_holds():
    w = World()
    held_before = len(w.supplier.records)
    case = w.mgr.handle(w.signal(SignalType.DELAY, delay=30, notice=None))
    assert case.severity is Severity.LOW and case.options == []
    assert case.notification["delivered_via"] == "push"
    assert len(w.supplier.records) == held_before


def test_expired_hold_is_refreshed_on_choose():
    w = World()
    case = w.mgr.handle(w.signal())
    w.clock[0] = NOW + timedelta(minutes=11)
    assert w.mgr.choose(case.case_id, case.options[0].option_id).status == "RESOLVED"


def test_unknown_option_and_double_resolution_rejected():
    w = World()
    case = w.mgr.handle(w.signal())
    with pytest.raises(KeyError):
        w.mgr.choose(case.case_id, "nope")
    w.mgr.choose(case.case_id, case.options[0].option_id)
    with pytest.raises(ValueError, match="RESOLVED"):
        w.mgr.choose(case.case_id, case.options[0].option_id)


def test_disruption_events_are_logged_on_the_booking():
    w = World()
    w.mgr.handle(w.signal())
    steps = [e["data"]["step"] for e in w.store.events("bk1") if e["type"] == "DISRUPTION"]
    assert steps[:3] == ["DETECTED", "OPTIONS", "NOTIFIED"]


def test_consent_fallthrough_and_no_channel():
    w = World(sender=RecordingSender(down={"push"}), channels=["push", "sms"])
    assert w.mgr.handle(w.signal(SignalType.DELAY, delay=30, notice=None)).notification["delivered_via"] == "sms"
    w2 = World(sender=RecordingSender(down={"push", "sms"}), channels=["push", "sms"])
    c = w2.mgr.handle(w2.signal(SignalType.DELAY, delay=30, notice=None))
    assert c.notification["delivered_via"] is None
    assert any("could not be reached" in n for n in c.notes)


# --- autonomy -----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({"cost_delta": 0, "pre": True}, Decision.AUTO_EXECUTE),
        ({"cost_delta": 0, "pre": False}, Decision.AUTO_EXECUTE_NOTIFY),
        ({"cost_delta": 12000}, Decision.AUTO_EXECUTE_NOTIFY),
        ({"cost_delta": 12001}, Decision.REQUIRE_APPROVAL),
        ({"cost_delta": 0, "same_carrier": False}, Decision.REQUIRE_APPROVAL),
        ({"cost_delta": 0, "downgrade": True}, Decision.REQUIRE_APPROVAL),
        ({"cost_delta": 0, "same_day": False}, Decision.REQUIRE_APPROVAL),
    ],
)
def test_autonomy_table(kw, expected):
    pre = kw.pop("pre", False)
    args = {"cost_delta": 0, "same_carrier": True, "same_day": True, "downgrade": False, **kw}
    d, _ = decide(**args, policy=AutonomyPolicy(pre_approved_same_carrier=pre))
    assert d is expected


# --- entitlements -----------------------------------------------------------------------
def sig(type_, delay=0, notice=5.0):
    return DisruptionSignal(
        signal_id="e", type=type_, flight_ref="r", delay_minutes=delay, notice_hours=notice, occurred_at=NOW
    )


def comp(ents):
    return next((e for e in ents if e.kind == "compensation"), None)


@pytest.mark.parametrize(("block", "amount"), [(50, 5000), (60, 5000), (90, 7500), (120, 7500), (150, 10000)])
def test_dgca_cancellation_amounts_by_block_time(block, amount):
    ents = entitlements(sig(SignalType.CANCELLATION), FlightContext(block_time_min=block))
    assert comp(ents).amount == amount and comp(ents).requires_approval


def test_dgca_cancellation_capped_at_fare():
    ents = entitlements(sig(SignalType.CANCELLATION), FlightContext(block_time_min=150, basic_fare_plus_fuel=3000))
    assert comp(ents).amount == 3000


@pytest.mark.parametrize(
    "ctx_kw",
    [{"alternate_departure_gap_min": 90}, {"extraordinary_circumstances": True}],
)
def test_dgca_no_compensation_when_alternate_or_extraordinary(ctx_kw):
    ents = entitlements(sig(SignalType.CANCELLATION), FlightContext(block_time_min=90, **ctx_kw))
    assert comp(ents) is None and any(e.kind == "refund_or_rebook" for e in ents)


def test_dgca_no_compensation_with_two_weeks_notice():
    assert comp(entitlements(sig(SignalType.CANCELLATION, notice=24 * 15), FlightContext(block_time_min=90))) is None


def test_dgca_delay_care_and_refund_thresholds():
    ctx = FlightContext(block_time_min=100)
    assert entitlements(sig(SignalType.DELAY, 90), ctx) == []
    assert any(e.kind == "care" for e in entitlements(sig(SignalType.DELAY, 120), ctx))
    long_ = entitlements(sig(SignalType.DELAY, 400, notice=30), ctx)
    assert any(e.kind == "refund_or_rebook" for e in long_)
    assert comp(long_) is None  # delays do not earn fixed compensation under DGCA


def test_dgca_denied_boarding_tiers():
    ctx = FlightContext(block_time_min=90, basic_fare_plus_fuel=4000, alternate_arrival_delay_min=300)
    assert comp(entitlements(sig(SignalType.DENIED_BOARDING), ctx)).amount == 8000  # 200% of fare
    ctx2 = FlightContext(block_time_min=90, basic_fare_plus_fuel=9000, alternate_arrival_delay_min=300)
    assert comp(entitlements(sig(SignalType.DENIED_BOARDING), ctx2)).amount == 10000  # capped
    ctx3 = FlightContext(block_time_min=90, basic_fare_plus_fuel=9000, alternate_arrival_delay_min=30 * 60)
    assert comp(entitlements(sig(SignalType.DENIED_BOARDING), ctx3)).amount == 20000  # 400%, cap 20000
    ctx4 = FlightContext(block_time_min=90, basic_fare_plus_fuel=9000, alternate_arrival_delay_min=45)
    assert comp(entitlements(sig(SignalType.DENIED_BOARDING), ctx4)) is None


@pytest.mark.parametrize(("km", "eur"), [(800, 250), (1500, 250), (2500, 400), (5000, 600)])
def test_eu261_tiers(km, eur):
    ctx = FlightContext(block_time_min=200, region="EU", distance_km=km, alternate_arrival_delay_min=300)
    c = comp(entitlements(sig(SignalType.CANCELLATION), ctx))
    assert (c.amount, c.currency, c.regime) == (eur, "EUR", "EU261")


def test_uk261_uses_gbp_and_extraordinary_blocks_claim():
    ctx = FlightContext(block_time_min=200, region="UK", distance_km=2000, alternate_arrival_delay_min=300)
    c = comp(entitlements(sig(SignalType.CANCELLATION), ctx))
    assert (c.amount, c.currency) == (350, "GBP")
    ctx.extraordinary_circumstances = True
    assert comp(entitlements(sig(SignalType.CANCELLATION), ctx)) is None


def test_case_includes_claim_draft_needing_approval():
    w = World()
    case = w.mgr.handle(w.signal())
    assert case.claims and all("Compensation claim" in c for c in case.claims)
    assert all(e.requires_approval for e in case.entitlements if e.kind == "compensation")
    ent = comp(case.entitlements)
    assert str(ent.amount) in draft_claim(ent, "bk1", "r")


# --- notify ----------------------------------------------------------------------------------
def test_notify_respects_order_and_consent():
    s = RecordingSender()
    assert notify("b", "m", ["email", "sms"], s)["delivered_via"] == "sms"  # order, not input order
    assert notify("b", "m", [], s)["delivered_via"] is None
    assert all(ch in {"sms"} for ch, _, _ in s.sent)


# --- detector ---------------------------------------------------------------------------------
def test_webhook_and_status_signals():
    s = detector.from_webhook({"event": "FLIGHT_DELAYED", "flight": "F1", "delay_min": 95}, NOW)
    assert s.type is SignalType.DELAY and s.delay_minutes == 95 and s.source == "webhook"
    assert detector.from_webhook({"event": "GATE_CHANGE", "flight": "F1"}, NOW) is None
    seen: set[str] = set()
    st = {"flight": "F1", "status": "DELAYED", "delay_min": 45}
    assert detector.from_status(st, NOW, seen) is not None
    assert detector.from_status(st, NOW, seen) is None  # same poll result is not a new event
    assert detector.from_status({"flight": "F1", "status": "ON_TIME"}, NOW) is None


class EmailLLM:
    def __init__(self, reply):
        self.reply = reply

    def complete(self, system, user):
        return self.reply


def test_email_parsing_validated_against_booked_flights():
    w = World()
    good = json.dumps({"type": "CANCELLATION", "flight_ref": w.out_ref, "delay_minutes": 0, "notice_hours": 3})
    s = detector.from_email("Your flight is cancelled", [w.trip], EmailLLM(good), NOW)
    assert s.type is SignalType.CANCELLATION and s.source == "email"
    bad_ref = json.dumps({"type": "CANCELLATION", "flight_ref": "XX-999", "delay_minutes": 0})
    assert detector.from_email("x", [w.trip], EmailLLM(bad_ref), NOW) is None  # hallucinated / injected
    assert detector.from_email("x", [w.trip], EmailLLM("not json at all"), NOW) is None
    assert detector.from_email("x", [w.trip], EmailLLM('{"type": null}'), NOW) is None


def test_match_trips_by_flight():
    w = World()
    assert detector.match_trips(w.signal(), [w.trip]) == [w.trip]
    assert detector.match_trips(w.signal(ref="nope"), [w.trip]) == []
