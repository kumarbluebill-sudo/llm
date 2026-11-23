from dataclasses import dataclass, field
from datetime import timedelta

from agents.knowledge import AIRPORTS
from api.schemas import Manifest
from mcp_servers.models import Activity, FlightOffer, Forecast, HotelOffer, Route
from mcp_servers.resilience import SupplierUnavailable
from mcp_servers.toolbox import Toolbox


class PricingError(Exception):
    pass


@dataclass
class PricedOptions:
    origin: str
    nights: int
    outbound: list[FlightOffer]
    inbound: list[FlightOffer]
    hotels: list[HotelOffer]
    route: Route
    forecast: Forecast
    notes: list[str] = field(default_factory=list)
    activities: list[Activity] = field(default_factory=list)


def _verified_flights(
    tools: Toolbox, origin: str, dest: str, day, manifest: Manifest, notes: list[str]
) -> list[FlightOffer]:
    c = manifest.constraints
    offers = tools.search_flights(origin, dest, day, c.refundable_only)
    kept = [o for o in offers if o.layovers <= c.max_layovers]
    if c.refundable_only:
        # The listing can be wrong; fare rules are authoritative. A source with NO fare rules
        # (live search data) cannot prove refundability: keep the fare but never claim it.
        verified = []
        for o in kept:
            rules = tools.get_fare_rules(o.offer_id)
            if rules.refundable or not rules.known:
                verified.append(o)
        if len(verified) < len(kept):
            notes.append(
                f"{origin}→{dest}: dropped {len(kept) - len(verified)} flight(s) "
                "with non-refundable fare rules"
            )
        kept = verified
    return kept


def price(manifest: Manifest, tools: Toolbox) -> PricedOptions:
    if manifest.event is None:
        raise PricingError("manifest has no destination/event to price")
    venue, event_day = manifest.event.venue, manifest.event.date
    origin = manifest.origin or ("Mumbai" if venue == "Delhi" else "Delhi")
    if venue not in AIRPORTS or origin not in AIRPORTS:
        raise PricingError(f"unsupported city: {venue if venue not in AIRPORTS else origin}")
    notes: list[str] = []
    out_day, back_day = event_day - timedelta(days=1), event_day + timedelta(days=1)
    outbound = _verified_flights(tools, AIRPORTS[origin], AIRPORTS[venue], out_day, manifest, notes)
    inbound = _verified_flights(tools, AIRPORTS[venue], AIRPORTS[origin], back_day, manifest, notes)
    hotels = tools.search_hotels(venue, manifest.constraints.refundable_only, out_day, back_day)
    if not (outbound and inbound and hotels):
        raise PricingError("no options satisfy the constraints (flights/hotels)")
    try:  # suggestions are optional: never let them break pricing
        activities = tools.search_activities(venue)
    except SupplierUnavailable:
        activities = []
        notes.append("Activity suggestions are temporarily unavailable")
    return PricedOptions(
        origin=origin,
        nights=(back_day - out_day).days,
        outbound=outbound,
        inbound=inbound,
        hotels=hotels,
        route=tools.get_route(AIRPORTS[venue], venue),
        forecast=tools.forecast(venue, event_day),
        notes=notes,
        activities=activities,
    )
