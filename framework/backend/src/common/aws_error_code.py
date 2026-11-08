"""The AWS error codes the backend branches on, so no caller compares raw error strings."""

from __future__ import annotations

from enum import Enum

from botocore.exceptions import ClientError


class AwsErrorCode(str, Enum):
    """`Error.Code` values, and DynamoDB transaction `CancellationReasons` codes, that change behavior."""

    CONDITIONAL_CHECK_FAILED = "ConditionalCheckFailedException"
    TRANSACTION_CANCELED = "TransactionCanceledException"
    CONDITIONAL_CHECK_FAILED_REASON = "ConditionalCheckFailed"
    """A transaction item's condition failed (a cancellation reason, not an error code)."""
    TRANSACTION_CONFLICT_REASON = "TransactionConflict"
    """Another transaction held an item (a cancellation reason, not an error code)."""
    THROTTLING_REASON = "ThrottlingError"
    """DynamoDB throttled a transaction item (a cancellation reason, not an error code)."""
    PROVISIONED_THROUGHPUT_EXCEEDED = "ProvisionedThroughputExceededException"
    REQUEST_LIMIT_EXCEEDED = "RequestLimitExceeded"
    THROTTLING = "ThrottlingException"
    STS_THROTTLING = "Throttling"
    ACCESS_DENIED = "AccessDenied"
    ACCESS_DENIED_EXCEPTION = "AccessDeniedException"
    VALIDATION = "ValidationException"
    EXECUTION_DOES_NOT_EXIST = "ExecutionDoesNotExist"
    EXECUTION_ALREADY_EXISTS = "ExecutionAlreadyExists"
    CONFLICT = "ConflictException"
    RESOURCE_NOT_FOUND = "ResourceNotFoundException"
    ENTITY_ALREADY_EXISTS = "EntityAlreadyExists"
    NO_SUCH_ENTITY = "NoSuchEntity"

    @classmethod
    def from_client_error(cls, error: ClientError) -> AwsErrorCode | None:
        """Return the error's code, or None for a code the backend doesn't branch on."""
        return cls.from_code(error.response.get("Error", {}).get("Code"))

    @classmethod
    def from_code(cls, code: object) -> AwsErrorCode | None:
        """Return the member for a raw code, or None when it isn't one."""
        try:
            return cls(code)
        except ValueError:
            return None

    @classmethod
    def cancellation_reasons(cls, error: ClientError) -> frozenset[AwsErrorCode | None]:
        """Return the codes of a canceled transaction's reasons, one per transaction item."""
        return frozenset(cls.ordered_cancellation_reasons(error))

    @classmethod
    def ordered_cancellation_reasons(cls, error: ClientError) -> tuple[AwsErrorCode | None, ...]:
        """Return a canceled transaction's reason codes in the order of its items."""
        reasons = error.response.get("CancellationReasons", [])
        return tuple(cls.from_code(reason.get("Code")) for reason in reasons)


THROTTLING_CODES = frozenset(
    {
        AwsErrorCode.PROVISIONED_THROUGHPUT_EXCEEDED,
        AwsErrorCode.REQUEST_LIMIT_EXCEEDED,
        AwsErrorCode.THROTTLING,
        AwsErrorCode.STS_THROTTLING,
        AwsErrorCode.THROTTLING_REASON,
    }
)
"""Codes that mean AWS throttled the call, so a later retry can succeed."""
