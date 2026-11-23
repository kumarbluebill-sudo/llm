class TransientError(Exception):
    """Timeouts / 5xx. Safe to retry because every mutating call carries an idempotency key."""


class SupplierRejected(Exception):
    """Business rejection (sold out, declined). Not retryable."""


class BookingAborted(Exception):
    """Saga stopped on purpose (approval rejected or timed out)."""
