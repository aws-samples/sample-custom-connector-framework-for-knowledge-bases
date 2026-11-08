"""The AWS error codes the runner branches on."""

from __future__ import annotations

from enum import Enum

from botocore.exceptions import ClientError


class AwsErrorCode(str, Enum):
    """An AWS error code; services spell an access denial either way."""

    ACCESS_DENIED = "AccessDenied"
    ACCESS_DENIED_EXCEPTION = "AccessDeniedException"

    @classmethod
    def denies(cls, error: ClientError) -> bool:
        """Return whether the error is an access denial."""
        code = error.response.get("Error", {}).get("Code")
        return code in {cls.ACCESS_DENIED.value, cls.ACCESS_DENIED_EXCEPTION.value}
