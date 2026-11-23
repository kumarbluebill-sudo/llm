"""Typed results returned by supplier tools."""
from datetime import date

from pydantic import BaseModel


class FlightOffer(BaseModel):
    offer_id: str
    origin: str
    dest: str
    day: date
    price: int  # INR per passenger
    refundable_listed: bool  # what the search listing claims
    layovers: int
    depart: str = "09:00"  # local HH:MM
    duration_min: int = 90
    carrier: str = "IndiGo"


class FareRules(BaseModel):
    offer_id: str
    refundable: bool  # authoritative; may differ from the listing
    cancellation_fee: int


class HotelOffer(BaseModel):
    hotel_id: str
    name: str
    stars: int
    nightly_price: int  # INR per room per night
    refundable: bool


class Route(BaseModel):
    airport: str
    venue: str
    minutes: int
    cost_inr: int  # per vehicle, one way


class Forecast(BaseModel):
    venue: str
    day: date
    summary: str
    rain_probability: float


class Activity(BaseModel):
    """A bookable-elsewhere experience (tour, activity). Suggested, never part of the saga."""

    product_code: str
    title: str
    from_price: int  # INR, per person, "from" price (not a quote)
    currency: str = "INR"
    duration_min: int | None = None
    rating: float | None = None
    reviews: int | None = None
    url: str | None = None
    image: str | None = None
    source: str = "mock"
