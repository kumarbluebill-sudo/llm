from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class Event(BaseModel):
    name: str
    venue: str
    date: date
    time: str | None = None


class Travelers(BaseModel):
    adults: int = Field(ge=1, default=1)
    children: int = Field(ge=0, default=0)


class Budget(BaseModel):
    tier: Literal["budget", "mid", "premium"] = "mid"
    max_total: int = Field(gt=0)
    currency: str = "INR"


class Constraints(BaseModel):
    refundable_only: bool = True
    max_layovers: int = Field(ge=0, default=0)
    arrival_buffer_hours: int = Field(ge=0, default=4)


class Manifest(BaseModel):
    """Planner output (doc section 6.1)."""

    trip_id: str
    intent: Literal["event_travel", "leisure", "business"] = "event_travel"
    event: Event | None = None
    travelers: Travelers = Travelers()
    budget: Budget
    constraints: Constraints = Constraints()
    segments: list[str] = []


class PlanRequest(BaseModel):
    query: str = Field(min_length=3)


class PlanResponse(BaseModel):
    status: Literal["stub"]
    query: str
    manifest: Manifest | None = None
