"""Rule-based passenger-rights engine. The rule engine decides eligibility and amounts; an LLM
may only draft wording. Every compensation claim requires human approval (doc section 9.3).

RULES_AS_OF: these tables are a summary written from memory of the public rules (DGCA CAR
Section 3 Series M Part IV, EU 261/2004, UK 261). They have NOT been legally verified and
must be checked against the current regulations before any claim is filed for real."""
from typing import Literal

from pydantic import BaseModel

from disruption.models import DisruptionSignal, SignalType

RULES_AS_OF = "unverified summary (2026-11); confirm against current regulation text"


class Entitlement(BaseModel):
    regime: str
    kind: Literal["compensation", "care", "refund_or_rebook"]
    amount: int | None = None  # None for non-monetary entitlements
    currency: str | None = None
    basis: str
    requires_approval: bool = False


class FlightContext(BaseModel):
    """Facts the rules need. `basic_fare_plus_fuel` is per passenger, in INR (for DGCA)."""

    block_time_min: int
    basic_fare_plus_fuel: int = 0
    distance_km: int = 0
    region: Literal["IN", "EU", "UK"] = "IN"
    alternate_departure_gap_min: int | None = None  # alt flight departs this long after original
    alternate_arrival_delay_min: int | None = None  # alt flight arrives this much later than original
    extraordinary_circumstances: bool = False
    departure_hour: int = 12  # local departure hour 0-23


def _dgca_cancellation_amount(block_min: int) -> int:
    if block_min <= 60:
        return 5000
    if block_min <= 120:
        return 7500
    return 10000


def _dgca(sig: DisruptionSignal, ctx: FlightContext) -> list[Entitlement]:
    out: list[Entitlement] = []
    reg = "DGCA"
    if sig.type is SignalType.CANCELLATION:
        out.append(Entitlement(regime=reg, kind="refund_or_rebook", basis="Alternate flight or full refund on cancellation"))
        short = sig.notice_hours is not None and sig.notice_hours < 24 * 14
        no_alt = ctx.alternate_departure_gap_min is None or ctx.alternate_departure_gap_min > 120
        if short and no_alt and not ctx.extraordinary_circumstances:
            cap = _dgca_cancellation_amount(ctx.block_time_min)
            amount = min(cap, ctx.basic_fare_plus_fuel) if ctx.basic_fare_plus_fuel else cap
            out.append(Entitlement(regime=reg, kind="compensation", amount=amount, currency="INR",
                                   basis="Cancellation under 2 weeks' notice, no alternate within 2h; capped at fare+fuel",
                                   requires_approval=True))  # fmt: skip
    elif sig.type is SignalType.DELAY:
        threshold = 120 if ctx.block_time_min <= 150 else 180 if ctx.block_time_min <= 300 else 240
        if sig.delay_minutes >= threshold:
            out.append(Entitlement(regime=reg, kind="care", basis="Meals and refreshments while waiting"))
        if sig.delay_minutes > 360 and (sig.notice_hours or 0) >= 24:
            out.append(Entitlement(regime=reg, kind="refund_or_rebook", basis="Delay over 6h with 24h notice: alternate flight or full refund"))
        night = ctx.departure_hour >= 20 or ctx.departure_hour < 3
        if sig.delay_minutes > 1440 or (sig.delay_minutes > 360 and night):
            out.append(Entitlement(regime=reg, kind="care", basis="Hotel accommodation with transfers"))
    elif sig.type is SignalType.DENIED_BOARDING:
        out.append(Entitlement(regime=reg, kind="refund_or_rebook", basis="Alternate flight or full refund"))
        arr = ctx.alternate_arrival_delay_min
        if arr is None or arr > 60:
            beyond_24h = arr is not None and arr > 24 * 60
            pct, cap = (4, 20000) if beyond_24h else (2, 10000)
            amount = min(cap, pct * ctx.basic_fare_plus_fuel) if ctx.basic_fare_plus_fuel else cap
            out.append(Entitlement(regime=reg, kind="compensation", amount=amount, currency="INR",
                                   basis=f"Denied boarding: {pct * 100}% of fare+fuel, max {cap}",
                                   requires_approval=True))  # fmt: skip
    return out


_EU_TIERS = {"EU": (250, 400, 600, "EUR"), "UK": (220, 350, 520, "GBP")}


def _eu_uk(sig: DisruptionSignal, ctx: FlightContext) -> list[Entitlement]:
    reg = "EU261" if ctx.region == "EU" else "UK261"
    short, mid, long_, cur = _EU_TIERS[ctx.region]
    amount = short if ctx.distance_km <= 1500 else mid if ctx.distance_km <= 3500 else long_
    out: list[Entitlement] = [
        Entitlement(regime=reg, kind="care", basis="Meals, refreshments and communication while waiting")
    ]
    eligible = False
    if sig.type is SignalType.CANCELLATION:
        out.append(Entitlement(regime=reg, kind="refund_or_rebook", basis="Re-routing or full refund"))
        eligible = (sig.notice_hours or 0) < 24 * 14 and (ctx.alternate_arrival_delay_min or 999) > 120
    elif sig.type is SignalType.DELAY:
        eligible = (ctx.alternate_arrival_delay_min or sig.delay_minutes) >= 180
    elif sig.type is SignalType.DENIED_BOARDING:
        eligible = True
    if eligible and not ctx.extraordinary_circumstances:
        out.append(Entitlement(regime=reg, kind="compensation", amount=amount, currency=cur,
                               basis=f"Fixed compensation tier for {ctx.distance_km} km", requires_approval=True))  # fmt: skip
    return out


def entitlements(sig: DisruptionSignal, ctx: FlightContext) -> list[Entitlement]:
    return _dgca(sig, ctx) if ctx.region == "IN" else _eu_uk(sig, ctx)


def draft_claim(ent: Entitlement, booking_id: str, flight_ref: str) -> str:
    """Template draft. An LLM could polish the wording, but amount/eligibility come from rules."""
    amt = f"{ent.currency} {ent.amount}" if ent.amount else "the entitlements below"
    return (
        f"Subject: Compensation claim - booking {booking_id}, flight {flight_ref}\n\n"
        f"I am claiming {amt} under {ent.regime} ({ent.basis}).\n"
        "Please confirm receipt and the expected settlement date."
    )
