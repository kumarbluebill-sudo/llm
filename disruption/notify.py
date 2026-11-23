"""Consent-ordered notification: push -> SMS -> WhatsApp -> email. Falls through on failure."""
from typing import Protocol

CHANNEL_ORDER = ["push", "sms", "whatsapp", "email"]


class Sender(Protocol):
    def send(self, channel: str, booking_id: str, message: str) -> None: ...


def notify(booking_id: str, message: str, consented: list[str], sender: Sender) -> dict:
    """Try consented channels in the fixed order. Never uses a channel without consent."""
    attempts: list[dict] = []
    for ch in (c for c in CHANNEL_ORDER if c in consented):
        try:
            sender.send(ch, booking_id, message)
        except Exception as e:  # noqa: BLE001 - any delivery failure falls through to next channel
            attempts.append({"channel": ch, "ok": False, "error": str(e)})
            continue
        attempts.append({"channel": ch, "ok": True})
        return {"delivered_via": ch, "attempts": attempts}
    return {"delivered_via": None, "attempts": attempts}  # needs a human to reach the traveller


class RecordingSender:
    """Mock sender; `down` channels raise."""

    def __init__(self, down: set[str] | None = None) -> None:
        self.down = down or set()
        self.sent: list[tuple[str, str, str]] = []

    def send(self, channel: str, booking_id: str, message: str) -> None:
        if channel in self.down:
            raise ConnectionError(f"{channel} unavailable")
        self.sent.append((channel, booking_id, message))
