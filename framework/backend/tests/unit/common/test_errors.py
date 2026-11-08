"""A failed AWS call becomes the throttling or the unavailable error, carrying its AWS code."""

import pytest
from botocore.exceptions import ClientError

from common.aws_error_code import THROTTLING_CODES
from common.errors import DependencyThrottledError, DependencyUnavailableError, FailedAwsCall


def client_error(code: str, reasons: list[str] | None = None) -> ClientError:
    response: dict[str, object] = {"Error": {"Code": code}}
    if reasons is not None:
        response["CancellationReasons"] = [{"Code": reason} for reason in reasons]
    return ClientError(response, "TransactWriteItems")  # type: ignore[arg-type]


@pytest.mark.parametrize("code", sorted(THROTTLING_CODES))
def test_to_error_given_a_throttling_code_then_returns_the_throttling_error_with_the_code(code: str) -> None:
    error = FailedAwsCall(error=client_error(code), action="Job lookup").to_error()

    assert type(error) is DependencyThrottledError
    assert error.aws_error_code == code


def test_to_error_given_a_throttled_transaction_item_then_returns_the_throttling_error() -> None:
    failed = FailedAwsCall(
        error=client_error("TransactionCanceledException", ["ThrottlingError"]), action="Job creation"
    )

    assert type(failed.to_error()) is DependencyThrottledError


def test_to_error_given_any_other_code_then_returns_the_unavailable_error_naming_the_action() -> None:
    error = FailedAwsCall(error=client_error("InternalServerError"), action="Job lookup").to_error()

    assert type(error) is DependencyUnavailableError
    assert (str(error), error.aws_error_code) == ("Job lookup failed", "InternalServerError")
