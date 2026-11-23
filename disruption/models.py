from datetime import date, datetime, time, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from api.schemas import Itinerary


class Severity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class SignalType(StrEnum):
    DELAY = "DELAY"
    CANCELLATION = "CANCELLATION"
    MISSED_CONNECTION = "MISSED_CONNECTION"
    DENIED_BOARDING = "DENIED_BOARDING"


class DisruptionSignal(BaseModel):
    signal_id: str
    type: SignalType
    flight_ref: str  # offer_id of the affected booked flight
    delay_minutes: int = Field(default=0, ge=0)
    notice_hours: float | None = None  # hours between the notice and scheduled departure
    source: Literal["webhook", "poll", "email", "user", "test"] = "test"
    occurred_at: datetime
    received_at: datetime | None = None


class TripNode(BaseModel):
    id: str
    kind: Literal["flight", "transfer", "hotel", "event"]
    start: datetime
    end: datetime
    depends_on: list[str] = []
    min_gap_min: int = 0  # required gap after the latest predecessor ends
    fixed: bool = False  # fixed-time nodes (event, flights) cannot slide
    ref: str | None = None  # offer_id for flights
    flight_iata: str | None = None  # real flight number (e.g. 6E329) once known; enables live polling
    carrier: str | None = None
    price: int = 0  # per-passenger fare (flights)
    latest_start: datetime | None = None  # hotel late check-in cutoff
    extension_dependents: list[str] = []  # hotel must be extended if these break


class Trip(BaseModel):
    booking_id: str
    nodes: list[TripNode]
    pax: int = 1
    event_buffer_min: int = 240
    consent_channels: list[str] = ["push", "sms", "whatsapp", "email"]
    pre_approved_same_carrier: bool = False
    refundable_booked: bool = True
    payment_token: str = ""

    def node(self, node_id: str) -> TripNode:
        return next(n for n in self.nodes if n.id == node_id)

    def node_for_flight(self, flight_ref: str) -> TripNode | None:
        return next((n for n in self.nodes if n.kind == "flight" and n.ref == flight_ref), None)

    @classmethod
    def from_itinerary(cls, it: Itinerary, booking_id: str, **kw) -> "Trip":
        seg = it.segments
        out, hotel, tin, ev, tout, back = seg[0], seg[1], seg[2], seg[3], seg[4], seg[5]

        def at(day: str, hhmm: str) -> datetime:
            h, m = map(int, hhmm.split(":"))
            return datetime.combine(date.fromisoformat(day), time(h, m))

        out_start = at(out.details["day"], out.details["depart"])
        out_end = out_start + timedelta(minutes=int(out.details["duration_min"]))
        tin_start = out_end + timedelta(minutes=45)
        tin_end = tin_start + timedelta(minutes=int(tin.details["minutes"]))
        checkin = max(tin_end, datetime.combine(tin_end.date(), time(14, 0)))
        ev_start = at(ev.details["date"], ev.details.get("time", "19:30"))
        back_start = at(back.details["day"], back.details["depart"])
        checkout = datetime.combine(back_start.date(), time(12, 0))
        ev_end = ev_start + timedelta(hours=4)
        tout_start = ev_end
        tout_end = tout_start + timedelta(minutes=int(tout.details["minutes"]))
        pax = max(1, round(out.cost / max(1, int(out.details["price"]))))
        nodes = [
            TripNode(id="flight_out", kind="flight", start=out_start, end=out_end, fixed=True,
                     ref=str(out.details["offer_id"]), carrier=str(out.details.get("carrier")),
                     price=int(out.details["price"])),
            TripNode(id="transfer_in", kind="transfer", start=tin_start, end=tin_end,
                     depends_on=["flight_out"], min_gap_min=45),
            TripNode(id="hotel", kind="hotel", start=checkin, end=checkout, depends_on=["transfer_in"],
                     latest_start=datetime.combine(tin_end.date(), time(23, 59)),
                     extension_dependents=["flight_return"], ref=str(hotel.details["hotel_id"])),
            TripNode(id="event", kind="event", start=ev_start, end=ev_end, fixed=True,
                     depends_on=["transfer_in", "hotel"]),
            TripNode(id="transfer_out", kind="transfer", start=tout_start, end=tout_end, depends_on=["event"]),
            TripNode(id="flight_return", kind="flight", start=back_start,
                     end=back_start + timedelta(minutes=int(back.details["duration_min"])),
                     depends_on=["transfer_out"], min_gap_min=120, fixed=True,
                     ref=str(back.details["offer_id"]), carrier=str(back.details.get("carrier")),
                     price=int(back.details["price"])),
        ]  # fmt: skip
        return cls(booking_id=booking_id, nodes=nodes, pax=pax,
                   event_buffer_min=int(kw.pop("event_buffer_min", 240)), **kw)
