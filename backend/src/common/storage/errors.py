"""Storage exceptions mapped to sanitized modeled API errors at the boundary."""


class StorageConflictError(Exception):
    """A resource exists but its state rejects the requested write."""


class StorageNotFoundError(Exception):
    """A tenant-scoped resource does not exist."""


class StorageOperationError(Exception):
    """A persistence operation failed unexpectedly."""
