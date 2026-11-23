"""Deterministic human-in-the-loop gates (doc section 5, Phase 5). Pure functions only."""

DEFAULT_HITL_TOTAL_INR = 160_000  # ~ the doc's $2,000 threshold
MAX_PRICE_DEVIATION = 0.02


def check_policy(req: dict, held_flights: list[dict], held_hotel: dict) -> list[str]:
    """Return the reasons a human must approve before money is authorized (empty = go)."""
    reasons: list[str] = []
    legs = [*req["flights"], req["hotel"]]
    held = [*held_flights, held_hotel]

    threshold = req.get("hitl_total_inr") or DEFAULT_HITL_TOTAL_INR
    total_held = sum(h["price"] for h in held)
    if total_held > threshold:
        reasons.append(f"Total {total_held} exceeds approval threshold {threshold}")

    for leg, h in zip(legs, held, strict=True):
        quoted = leg["quoted"]
        if quoted and abs(h["price"] - quoted) / quoted > MAX_PRICE_DEVIATION:
            reasons.append(f"Supplier price {h['price']} deviates >2% from quote {quoted}")

    refundable = {leg["refundable"] for leg in legs}
    if False in refundable and not req.get("acknowledged_nonrefundable"):
        reasons.append("Non-refundable penalty not yet acknowledged")
    if len(refundable) > 1:
        reasons.append("Conflicting cancellation terms across suppliers")
    return reasons
