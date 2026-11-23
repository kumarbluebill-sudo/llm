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
