from enum import StrEnum

from pydantic import BaseModel, Field

from api.schemas import Itinerary


class State(StrEnum):
    DRAFT = "DRAFT"
    QUOTED = "QUOTED"
    HELD = "HELD"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    AUTHORIZED = "AUTHORIZED"
    CONFIRMED = "CONFIRMED"
    TICKETED = "TICKETED"
    COMPENSATING = "COMPENSATING"
    CANCELLED = "CANCELLED"
    NEEDS_MANUAL = "NEEDS_MANUAL"


TERMINAL = {State.TICKETED, State.CANCELLED, State.NEEDS_MANUAL}


class FlightLeg(BaseModel):
    offer_id: str
    quoted: int
    refundable: bool = True


class HotelStay(BaseModel):
    hotel_id: str
    nights: int
    quoted: int
    refundable: bool = True


class BookingRequest(BaseModel):
    """What the saga needs. Carries a payment *token* only - never card data."""

    booking_id: str
    flights: list[FlightLeg]
    hotel: HotelStay
    currency: str = "INR"
    payment_token: str
    acknowledged_nonrefundable: bool = False
    approval_timeout_s: int = Field(default=86400, gt=0)
    hitl_total_inr: int | None = None  # override policy defaults (mainly for tests)

    @property
    def total_quoted(self) -> int:
        return sum(f.quoted for f in self.flights) + self.hotel.quoted

    @classmethod
    def from_itinerary(
        cls, it: Itinerary, payment_token: str, booking_id: str, **overrides
    ) -> "BookingRequest":
        # Only flights and hotel are bookable here; transfers/events are not supplier-booked yet.
        flights = [
            FlightLeg(
                offer_id=str(s.details["offer_id"]), quoted=s.cost, refundable=bool(s.refundable)
            )
            for s in it.segments
            if s.type == "flight"
        ]
        hotel_seg = next(s for s in it.segments if s.type == "hotel")
        hotel = HotelStay(
            hotel_id=str(hotel_seg.details["hotel_id"]),
            nights=int(hotel_seg.details["nights"]),
            quoted=hotel_seg.cost,
            refundable=bool(hotel_seg.refundable),
        )
        return cls(
            booking_id=booking_id,
            flights=flights,
            hotel=hotel,
            currency=it.currency,
            payment_token=payment_token,
            **overrides,
        )
