"""AWS error codes resolve to enum members, and unknown codes to None."""

from botocore.exceptions import ClientError

from common.aws_error_code import AwsErrorCode


def client_error(code: str | None, reasons: list[str] | None = None) -> ClientError:
    response: dict[str, object] = {"Error": {} if code is None else {"Code": code}}
    if reasons is not None:
        response["CancellationReasons"] = [{"Code": reason} for reason in reasons]
    return ClientError(response, "TransactWriteItems")  # type: ignore[arg-type]


def test_from_client_error_returns_the_member_for_a_known_code() -> None:
    assert AwsErrorCode.from_client_error(client_error("ConditionalCheckFailedException")) is (
        AwsErrorCode.CONDITIONAL_CHECK_FAILED
    )


def test_from_client_error_returns_none_for_an_unknown_or_missing_code() -> None:
    assert AwsErrorCode.from_client_error(client_error("InternalServerError")) is None
    assert AwsErrorCode.from_client_error(client_error(None)) is None


def test_cancellation_reasons_lists_each_item_code() -> None:
    error = client_error("TransactionCanceledException", ["None", "ConditionalCheckFailed"])

    assert AwsErrorCode.cancellation_reasons(error) == {None, AwsErrorCode.CONDITIONAL_CHECK_FAILED_REASON}
