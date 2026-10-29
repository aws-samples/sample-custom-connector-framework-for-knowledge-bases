"""Deployment-owned backend environment fallback tests."""

import importlib

import pytest

import common.env as backend_env

_IDENTITY_VARIABLES = (
    "AWS_PARTITION",
    "FRAMEWORK_ACCOUNT_ID",
    "CCF_SERVICE_PRINCIPAL",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "CONFIGURATION_KEY_ARN",
)


def test_configuration_key_arn_supplies_framework_identity_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as environment:
        for variable_name in _IDENTITY_VARIABLES:
            environment.delenv(variable_name, raising=False)
        environment.setenv(
            "CONFIGURATION_KEY_ARN",
            "arn:aws-us-gov:kms:us-gov-west-1:999999999999:key/key-id",
        )
        environment.setenv("AWS_DEFAULT_REGION", "us-gov-west-1")

        loaded_env = importlib.reload(backend_env)

        assert loaded_env.FRAMEWORK_ACCOUNT_ID == "999999999999"
        assert loaded_env.AWS_PARTITION == "aws-us-gov"
        assert loaded_env.AWS_REGION == "us-gov-west-1"
        assert loaded_env.CCF_SERVICE_PRINCIPAL == "ccf.amazonaws.com"
    importlib.reload(backend_env)


def test_explicit_framework_identity_and_primary_region_win(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as environment:
        environment.setenv(
            "CONFIGURATION_KEY_ARN",
            "arn:aws:kms:us-east-1:111111111111:key/key-id",
        )
        environment.setenv("FRAMEWORK_ACCOUNT_ID", "222222222222")
        environment.setenv("AWS_PARTITION", "aws-iso")
        environment.setenv("AWS_REGION", "us-iso-east-1")
        environment.setenv("AWS_DEFAULT_REGION", "us-west-2")
        environment.setenv("CCF_SERVICE_PRINCIPAL", "configured.example.com")

        loaded_env = importlib.reload(backend_env)

        assert loaded_env.FRAMEWORK_ACCOUNT_ID == "222222222222"
        assert loaded_env.AWS_PARTITION == "aws-iso"
        assert loaded_env.AWS_REGION == "us-iso-east-1"
        assert loaded_env.CCF_SERVICE_PRINCIPAL == "configured.example.com"
    importlib.reload(backend_env)


@pytest.mark.parametrize(
    "configuration_key_arn",
    [
        "not-an-arn",
        "arn:aws:s3:us-east-1:123456789012:bucket/name",
        "arn:aws:kms:us-east-1:not-an-account:key/key-id",
    ],
)
def test_malformed_or_non_kms_arn_does_not_supply_identity(
    configuration_key_arn: str,
) -> None:
    assert backend_env._configuration_key_identity(configuration_key_arn) == ("", "")
