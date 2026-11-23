"""DETECT -> TRIAGE -> IMPACT -> OPTIONS -> ENTITLE -> NOTIFY -> EXECUTE (bounded autonomy)."""
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from api.telemetry import span
from disruption.autonomy import AutonomyPolicy, Decision
from disruption.entitlements import Entitlement, FlightContext, draft_claim, entitlements
from disruption.impact import ImpactReport, analyze
from disruption.models import DisruptionSignal, Severity, SignalType, Trip
from disruption.notify import Sender, notify
from disruption.options import RebookOption, build_options
from disruption.triage import ACTIONS, triage
from mcp_servers.toolbox import Toolbox
from saga.activities import Activities, idempotent

_UNAVAILABLE = {SignalType.CANCELLATION, SignalType.MISSED_CONNECTION, SignalType.DENIED_BOARDING}


class DisruptionCase(BaseModel):
    case_id: str
    booking_id: str
    signal: DisruptionSignal
    severity: Severity
    action: str
    impact: ImpactReport
    options: list[RebookOption] = []
    entitlements: list[Entitlement] = []
    claims: list[str] = []
    notification: dict = {}
    status: str = "OPEN"  # OPEN | AWAITING_APPROVAL | RESOLVED | DISMISSED | EXEC_FAILED | NEEDS_MANUAL
    chosen_option: str | None = None
    notes: list[str] = []
    metrics: dict[str, float] = {}


class DisruptionManager:
    def __init__(
        self,
        trips: dict[str, Trip],
        toolbox: Toolbox,
        activities: Activities,
        sender: Sender,
        policy: AutonomyPolicy | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.trips, self.toolbox, self.acts, self.sender = trips, toolbox, activities, sender
        self.policy = policy or AutonomyPolicy()
        self._clock = clock
        self.cases: dict[str, DisruptionCase] = {}

    # --- helpers -----------------------------------------------------------
    def _log(self, case: DisruptionCase, step: str, **data: Any) -> None:
        self.acts.store.append(case.booking_id, "DISRUPTION", None, {"case": case.case_id, "step": step, **data})

    def _hold_prefix(self, booking_id: str, case_id: str) -> str:
        return f"{booking_id}:disr-{case_id}"

    def _hold_fn(self, trip: Trip, case_id: str) -> Callable[[RebookOption], dict | None]:
        def hold(o: RebookOption) -> dict | None:
            try:
                return self.acts.hold_flight(
                    {"booking_id": self._hold_prefix(trip.booking_id, case_id), "offer_id": o.offer.offer_id,
                     "quoted": o.offer.price * trip.pax}
                )  # fmt: skip
            except Exception:  # noqa: BLE001 - a failed hold just removes the option
                return None

        return hold

    def _release(self, case: DisruptionCase, keep: str | None = None) -> None:
        for o in case.options:
            if o.hold_id and o.option_id != keep:
                try:
                    self.acts.cancel_flight(
                        {"booking_id": self._hold_prefix(case.booking_id, case.case_id), "hold_key": o.hold_id}
                    )
                except Exception as e:  # noqa: BLE001
                    case.notes.append(f"could not release hold {o.hold_id}: {e}")

    # --- pipeline ------------------------------------------------------------
    def handle(self, sig: DisruptionSignal) -> DisruptionCase:
        with span("disruption.handle", signal_type=sig.type.value, source=sig.source) as sp:
            case = self._handle(sig)
            sp.set_attribute("severity", case.severity.value)
            sp.set_attribute("options", len(case.options))
            sp.set_attribute("status", case.status)
            return case

    def _handle(self, sig: DisruptionSignal) -> DisruptionCase:
        trip = next(t for t in self.trips.values() if t.node_for_flight(sig.flight_ref))
        node = trip.node_for_flight(sig.flight_ref)
        assert node is not None
        received = sig.received_at or self._clock()
        case_id = uuid.uuid4().hex[:12]

        severity = triage(sig)  # TRIAGE (deterministic)
        impact = analyze(  # IMPACT (whole graph, no cascade miss)
            trip, node.id, cancelled=sig.type in _UNAVAILABLE, delay_min=sig.delay_minutes
        )
        case = DisruptionCase(
            case_id=case_id, booking_id=trip.booking_id, signal=sig, severity=severity,
            action=ACTIONS[severity], impact=impact,
        )  # fmt: skip
        self.cases[case_id] = case
        self._log(case, "DETECTED", signal=sig.type.value, severity=severity.value, broken=impact.broken)

        if severity is not Severity.LOW:  # OPTIONS with inventory holds
            with span("disruption.options"):
                case.options = build_options(
                    trip, node, self.toolbox, self.policy, self._hold_fn(trip, case_id), self._clock()
                )
            first_option_at = self._clock()
            case.metrics["time_to_first_option_s"] = (first_option_at - received).total_seconds()
            self._log(case, "OPTIONS", options=[o.option_id for o in case.options])
            if not case.options:
                case.notes.append("No feasible alternative found; human agent required")

        case.metrics["time_to_detection_s"] = (received - sig.occurred_at).total_seconds()

        ctx = self._flight_context(sig, node, case.options)  # ENTITLE (rule engine)
        case.entitlements = entitlements(sig, ctx)
        case.claims = [
            draft_claim(e, trip.booking_id, sig.flight_ref) for e in case.entitlements if e.kind == "compensation"
        ]

        with span("disruption.notify"):
            case.notification = notify(  # NOTIFY (consent-ordered)
                trip.booking_id, self._message(case), trip.consent_channels, self.sender
            )
        if case.notification["delivered_via"] is None:
            case.notes.append("Traveller could not be reached on any consented channel")
        self._log(case, "NOTIFIED", via=case.notification["delivered_via"])

        top = case.options[0] if case.options else None  # EXECUTE (bounded autonomy)
        if top and severity in (Severity.HIGH, Severity.CRITICAL) and top.decision in (
            Decision.AUTO_EXECUTE, Decision.AUTO_EXECUTE_NOTIFY
        ) and sig.type is not SignalType.DENIED_BOARDING:  # fmt: skip
            self.execute(case, top)
        elif case.options:
            case.status = "AWAITING_APPROVAL"
            self._log(case, "AWAITING_APPROVAL", reasons=top.decision_reasons if top else [])
        return case

    def _flight_context(self, sig: DisruptionSignal, node, options: list[RebookOption]) -> FlightContext:
        best = options[0] if options else None
        return FlightContext(
            block_time_min=int((node.end - node.start).total_seconds() // 60),
            basic_fare_plus_fuel=node.price,
            region="IN",
            alternate_departure_gap_min=int((best.depart_at - node.start).total_seconds() // 60) if best else None,
            alternate_arrival_delay_min=int((best.arrive_at - node.end).total_seconds() // 60) if best else None,
            departure_hour=node.start.hour,
        )

    def _message(self, case: DisruptionCase) -> str:
        s = case.signal
        lines = [f"Flight {s.flight_ref}: {s.type.value.lower()} ({case.severity.value})."]
        bad = [i for i in case.impact.impacts if i.status in ("broken", "at_risk")]
        if bad:
            lines.append("Affected: " + ", ".join(f"{i.node_id} ({i.status})" for i in bad))
        if case.options:
            lines.append(f"{len(case.options)} alternatives held for 10 minutes.")
        return " ".join(lines)

    # --- decisions -------------------------------------------------------------
    def choose(self, case_id: str, option_id: str) -> DisruptionCase:
        """The traveller approved this option."""
        case = self.cases[case_id]
        if case.status not in ("AWAITING_APPROVAL", "OPEN"):
            raise ValueError(f"case is {case.status}")
        option = next((o for o in case.options if o.option_id == option_id), None)
        if option is None:
            raise KeyError(option_id)
        if option.hold_expires_at and self._clock() > option.hold_expires_at:
            fresh = self._hold_fn(self.trips[case.booking_id], case_id)(option)
            if fresh is None:
                raise ValueError("hold expired and inventory is no longer available")
        return self.execute(case, option)

    def dismiss(self, case_id: str) -> DisruptionCase:
        case = self.cases[case_id]
        self._release(case)
        case.status = "DISMISSED"
        self._log(case, "DISMISSED")
        return case

    def execute(self, case: DisruptionCase, option: RebookOption) -> DisruptionCase:
        trip = self.trips[case.booking_id]
        node = trip.node_for_flight(case.signal.flight_ref)
        assert node is not None and option.hold_id is not None
        case.chosen_option = option.option_id
        self._log(case, "EXEC_STARTED", option=option.option_id)
        replaced_key = f"{case.booking_id}:flight:{node.ref}"
        prefix = self._hold_prefix(case.booking_id, case.case_id)
        try:
            self.acts.ticket_flight({"booking_id": prefix, "hold_key": option.hold_id})
        except Exception as e:  # noqa: BLE001
            # Never improvise a different booking: release holds and hand over to a human.
            self._release(case)
            case.status = "EXEC_FAILED"
            case.notes.append(f"ticketing the new flight failed: {e}")
            self._log(case, "EXEC_FAILED", error=str(e))
            return case

        old_key = f"{case.booking_id}:flight:{node.ref}"
        try:
            self.acts.refund_ticket({"booking_id": case.booking_id, "hold_key": old_key})
        except Exception as e:  # noqa: BLE001
            case.status = "NEEDS_MANUAL"
            case.notes.append(f"new ticket issued but refunding the old ticket failed: {e}")

        if option.cost_delta > 0:
            self._charge_delta(case, trip, option.cost_delta)
        self._release(case, keep=option.option_id)

        node.ref, node.carrier, node.price = option.offer.offer_id, option.offer.carrier, option.offer.price
        node.start, node.end = option.depart_at, option.arrive_at
        if case.status not in ("NEEDS_MANUAL",):
            case.status = "RESOLVED"
        self._log(case, "RESOLVED", status=case.status, new_key=option.hold_id, replaced_key=replaced_key)
        return case

    def _charge_delta(self, case: DisruptionCase, trip: Trip, amount: int) -> None:
        if not trip.payment_token:
            case.status = "NEEDS_MANUAL"
            case.notes.append(f"fare difference {amount} could not be charged (no payment token)")
            return
        key = f"{case.booking_id}:delta:{case.case_id}"
        try:
            idempotent(self.acts.store, f"{key}:auth", lambda _: self.acts.payments.authorize(key, amount, trip.payment_token))
            idempotent(self.acts.store, f"{key}:capture", lambda _: self.acts.payments.capture(key))
        except Exception as e:  # noqa: BLE001
            case.status = "NEEDS_MANUAL"
            case.notes.append(f"charging fare difference {amount} failed: {e}")
