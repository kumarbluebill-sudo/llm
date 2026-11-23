import math
from dataclasses import dataclass

from agents.pricer import PricedOptions
from api.schemas import Manifest
from mcp_servers.models import FlightOffer, HotelOffer

TIERS = ("budget", "mid", "premium")


@dataclass
class TierQuote:
    tier: str
    outbound: FlightOffer
    inbound: FlightOffer
    hotel: HotelOffer
    flights_cost: int
    hotel_cost: int
    transfer_cost: int
    total: int
    within_budget: bool


def _pick(items: list, tier: str):
    ordered = items  # callers pass price-sorted lists
    idx = {"budget": 0, "mid": len(ordered) // 2, "premium": len(ordered) - 1}[tier]
    return ordered[idx]


def quote_tiers(manifest: Manifest, opts: PricedOptions) -> dict[str, TierQuote]:
    pax = manifest.travelers.adults + manifest.travelers.children
    rooms = math.ceil(pax / 2)
    vehicles = math.ceil(pax / 4)
    out = sorted(opts.outbound, key=lambda o: o.price)
    back = sorted(opts.inbound, key=lambda o: o.price)
    hotels = sorted(opts.hotels, key=lambda h: h.nightly_price)
    quotes = {}
    for tier in TIERS:
        o, b, h = _pick(out, tier), _pick(back, tier), _pick(hotels, tier)
        flights_cost = (o.price + b.price) * pax
        hotel_cost = h.nightly_price * opts.nights * rooms
        transfer_cost = opts.route.cost_inr * 2 * vehicles
        total = flights_cost + hotel_cost + transfer_cost
        quotes[tier] = TierQuote(
            tier, o, b, h, flights_cost, hotel_cost, transfer_cost, total,
            total <= manifest.budget.max_total,
        )  # fmt: skip
    return quotes
