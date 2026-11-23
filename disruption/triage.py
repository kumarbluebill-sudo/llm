"""Deterministic severity rules (doc section 9.2). No LLM."""
from disruption.models import DisruptionSignal, Severity, SignalType

ACTIONS = {
    Severity.LOW: "notify_only",
    Severity.MEDIUM: "offer_alternatives",
    Severity.HIGH: "generate_rebooking_options",
    Severity.CRITICAL: "hitl_and_hold_alternatives",
}


def triage(sig: DisruptionSignal) -> Severity:
    if sig.type is SignalType.DENIED_BOARDING:
        return Severity.CRITICAL
    if sig.type is SignalType.MISSED_CONNECTION:
        return Severity.HIGH
    if sig.type is SignalType.CANCELLATION:
        short_notice = sig.notice_hours is None or sig.notice_hours < 24
        return Severity.CRITICAL if short_notice else Severity.HIGH
    # DELAY
    if sig.delay_minutes < 60:
        return Severity.LOW
    if sig.delay_minutes <= 180:
        return Severity.MEDIUM
    return Severity.HIGH
