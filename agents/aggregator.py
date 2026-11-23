import uuid

from agents.budget import TIERS, TierQuote, quote_tiers
from agents.pricer import PricedOptions
from api.schemas import BookingAction, Itinerary, Manifest, Segment, Suggestion


def _choose(manifest: Manifest, quotes: dict[str, TierQuote], warnings: list[str]) -> TierQuote:
    wanted = manifest.budget.tier
    # Requested tier first, then step down to cheaper tiers that fit the budget.
    order = list(reversed(TIERS[: TIERS.index(wanted) + 1]))
    for tier in order:
        if quotes[tier].within_budget:
            if tier != wanted:
                warnings.append(f"'{wanted}' tier exceeds budget; showing '{tier}' instead")
            return quotes[tier]
    warnings.append("No tier fits the budget; showing the cheapest option")
    return quotes["budget"]


def _suggestions(opts: PricedOptions) -> list[Suggestion]:
    ranked = sorted(opts.activities, key=lambda x: (-(x.rating or 0), x.from_price))
    return [
        Suggestion(code=x.product_code, title=x.title, from_price=x.from_price, currency=x.currency,
                   duration_min=x.duration_min, rating=x.rating, reviews=x.reviews, url=x.url,
                   image=x.image, source=x.source)
        for x in ranked[:6]
    ]  # fmt: skip


def aggregate(manifest: Manifest, opts: PricedOptions) -> Itinerary:
    assert manifest.event is not None  # guaranteed by pricer
    quotes = quote_tiers(manifest, opts)
    warnings = list(opts.notes)
    q = _choose(manifest, quotes, warnings)
    pax = manifest.travelers.adults + manifest.travelers.children
    ev = manifest.event
    if opts.forecast.rain_probability >= 0.6:
        warnings.append(f"Weather advisory for {ev.venue}: {opts.forecast.summary}")
    warnings.append("Event/match tickets are not included in the total")
    if opts.activities:
        warnings.append("Activity suggestions are not in the total and are booked on the provider site")

    refundable = manifest.constraints.refundable_only
    segments = [
        Segment(type="flight", details={"direction": "out", **q.outbound.model_dump(mode="json")},
                refundable=refundable, cost=q.outbound.price * pax),
        Segment(type="hotel", details={**q.hotel.model_dump(mode="json"), "nights": opts.nights},
                refundable=q.hotel.refundable, cost=q.hotel_cost),
        Segment(type="transfer", details={**opts.route.model_dump(mode="json"), "direction": "in"},
                cost=q.transfer_cost // 2),
        Segment(type="event", details={"name": ev.name, "venue": ev.venue, "date": ev.date.isoformat(),
                                    "time": ev.time or "19:30"}),
        Segment(type="transfer", details={**opts.route.model_dump(mode="json"), "direction": "out"},
                cost=q.transfer_cost // 2),
        Segment(type="flight", details={"direction": "return", **q.inbound.model_dump(mode="json")},
                refundable=refundable, cost=q.inbound.price * pax),
    ]  # fmt: skip
    return Itinerary(
        itinerary_id=str(uuid.uuid4()),
        summary=f"{opts.nights}-night {ev.venue} trip: {ev.name} ({q.tier} tier)",
        tier=q.tier,
        total_cost=q.total,
        within_budget=q.within_budget,
        tier_totals={t: quotes[t].total for t in TIERS},
        segments=segments,
        warnings=warnings,
        suggestions=_suggestions(opts),
        booking_actions=[
            BookingAction(label="Book all", action="commit_saga"),
            BookingAction(label="Book flights only", action="partial_commit"),
        ],
    )
