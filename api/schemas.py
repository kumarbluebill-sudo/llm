from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class Event(BaseModel):
    name: str
    venue: str
    date: date
    time: str | None = None


class Travelers(BaseModel):
    adults: int = Field(ge=1, le=20, default=1)
    children: int = Field(ge=0, le=20, default=0)


class Budget(BaseModel):
    tier: Literal["budget", "mid", "premium"] = "mid"
    max_total: int = Field(gt=0, le=10_000_000)
    currency: str = "INR"


class Constraints(BaseModel):
    refundable_only: bool = True
    max_layovers: int = Field(ge=0, default=0)
    arrival_buffer_hours: int = Field(ge=0, default=4)


class Manifest(BaseModel):
    """Planner output (doc section 6.1)."""

    trip_id: str
    intent: Literal["event_travel", "leisure", "business"] = "event_travel"
    origin: str | None = None
    event: Event | None = None
    travelers: Travelers = Travelers()
    budget: Budget
    constraints: Constraints = Constraints()
    segments: list[str] = []


class Segment(BaseModel):
    type: Literal["flight", "hotel", "transfer", "event"]
    details: dict
    refundable: bool | None = None
    cost: int = 0


class BookingAction(BaseModel):
    label: str
    action: str


class Suggestion(BaseModel):
    """Optional things to do. Not part of the total and not bookable through our saga."""

    code: str
    title: str
    from_price: int
    currency: str = "INR"
    duration_min: int | None = None
    rating: float | None = None
    reviews: int | None = None
    url: str | None = None
    image: str | None = None
    source: str


class Itinerary(BaseModel):
    """Aggregator output (doc section 6.3)."""

    itinerary_id: str
    summary: str
    tier: Literal["budget", "mid", "premium"]
    total_cost: int
    currency: str = "INR"
    within_budget: bool
    tier_totals: dict[str, int]
    segments: list[Segment]
    warnings: list[str] = []
    booking_actions: list[BookingAction] = []
    suggestions: list[Suggestion] = []
    data_source: Literal["sample", "mixed", "live"] = "sample"
    bookable: bool = True
    bookable_reason: str | None = None


class PlanRequest(BaseModel):
    query: str = Field(min_length=3, max_length=500)


class PlanResponse(BaseModel):
    status: Literal["ok"]
    query: str
    manifest: Manifest | None = None
    itinerary: Itinerary | None = None
