class RetentionError(ValueError):
    """Base error for retention planning."""


class RetentionInputError(RetentionError):
    """Raised when retention input is inconsistent or cannot be evaluated."""
