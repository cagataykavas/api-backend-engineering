class IdempotencyConflict(ValueError):
    """Raised when an idempotency key is replayed with a different payload."""


class InvalidCursor(ValueError):
    """Raised when a pagination cursor cannot be decoded safely."""
