"""Bounded autonomy (doc section 6, Phase 6). Pure function; the executor never overrides it."""
from enum import StrEnum

from pydantic import BaseModel


class Decision(StrEnum):
    AUTO_EXECUTE = "AUTO_EXECUTE"
    AUTO_EXECUTE_NOTIFY = "AUTO_EXECUTE_NOTIFY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class AutonomyPolicy(BaseModel):
    auto_delta_inr: int = 12_000  # ~ the doc's $150
    pre_approved_same_carrier: bool = False


def decide(
    *,
    cost_delta: int,
    same_carrier: bool,
    same_day: bool,
    downgrade: bool,
    policy: AutonomyPolicy,
) -> tuple[Decision, list[str]]:
    reasons: list[str] = []
    if not same_carrier:
        reasons.append("Cross-carrier rebooking always needs approval")
    if downgrade:
        reasons.append("Option is a downgrade (loses refundability)")
    if not same_day:
        reasons.append("Option changes the travel date")
    if reasons:
        return Decision.REQUIRE_APPROVAL, reasons
    if cost_delta <= 0 and policy.pre_approved_same_carrier:
        return Decision.AUTO_EXECUTE, []
    if cost_delta <= policy.auto_delta_inr:
        return Decision.AUTO_EXECUTE_NOTIFY, []
    return Decision.REQUIRE_APPROVAL, [f"Cost increase {cost_delta} exceeds auto limit {policy.auto_delta_inr}"]


# Refund and compensation claims are never auto-executed.
CLAIMS_REQUIRE_APPROVAL = True
