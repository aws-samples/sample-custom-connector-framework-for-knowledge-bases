"""Configuration boundary tests for validation and encryption context."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr

from common.configuration import (
    ConfigurationDecryptionError,
    ConfigurationValidationError,
    ConfigurationValidator,
    DecryptConfigurationRequest,
    KmsConfigurationCodec,
)

# Credential-shaped fixtures are assembled at runtime so secret scanners and
# repository push protection don't flag this test file.
FAKE_AWS_ACCESS_KEY_ID = "AKIA" + "IOSFODNN7EXAMPLE"
FAKE_PRIVATE_KEY_HEADER = "-----BEGIN " + "PRIVATE KEY-----"
FAKE_GITHUB_TOKEN = "ghp_" + "1" * 36
FAKE_GITLAB_TOKEN = "glpat-" + "1" * 24


def json_with_exact_utf8_size(character: str, byte_count: int) -> str:
    """Build a JSON object with exactly the requested UTF-8 byte size."""
    wrapper_size = len(b'{"value":""}')
    content_bytes = byte_count - wrapper_size
    encoded_character_size = len(character.encode())
    repeated, ascii_remainder = divmod(content_bytes, encoded_character_size)
    configuration = f'{{"value":"{character * repeated}{"a" * ascii_remainder}"}}'
    assert len(configuration.encode()) == byte_count
    return configuration


@pytest.mark.parametrize("character", ["a", "€"])
def test_validate_exact_utf8_limit_accepts(character: str) -> None:
    configuration = json_with_exact_utf8_size(character, 65_536)

    validated = ConfigurationValidator().validate(SecretStr(configuration))

    assert validated.get_secret_value() == configuration


@pytest.mark.parametrize("character", ["a", "€"])
def test_validate_over_utf8_limit_rejects(character: str) -> None:
    configuration = json_with_exact_utf8_size(character, 65_537)

    with pytest.raises(ConfigurationValidationError, match="65,536-byte"):
        ConfigurationValidator().validate(SecretStr(configuration))


@pytest.mark.parametrize("configuration", ["{", "[]", '"value"', "null"])
def test_validate_malformed_or_non_object_json_rejects(configuration: str) -> None:
    validator = ConfigurationValidator()

    with pytest.raises(ConfigurationValidationError):
        validator.validate(SecretStr(configuration))


@pytest.mark.parametrize(
    "configuration",
    [
        '{"password":"literal-value"}',
        '{"nested":{"api_key":"literal-value"}}',
        '{"auth":"Basic literal-value"}',
        '{"nested":{"authorization":"Basic literal-value"}}',
        '{"oauth":{"token":"literal-value"}}',
        '{"headers":{"Authorization":"Basic literal-value"}}',
        '{"auth_headers":{"X-Api-Key":"literal-value"}}',
        '{"keys":["literal-value"]}',
        '{"auth":{"value":"literal"}}',
        '{"auth":{}}',
        '{"auth":[]}',
        '{"auth":{"nested":{}}}',
        '{"auth":[{"token_secret_name":"connectors/token"},"literal"]}',
        '{"authorization":{"value":"literal"}}',
        '{"oauth":{"client":"literal"}}',
        '{"api_key_value":"literal"}',
        '{"key_value":"literal"}',
        '{"value_api_key":"literal"}',
        '{"value_key":"literal"}',
        f'{{"value":"{FAKE_AWS_ACCESS_KEY_ID}"}}',
        f'{{"private_key":"{FAKE_PRIVATE_KEY_HEADER}"}}',
        f'{{"token_secret_arn":"{FAKE_GITHUB_TOKEN}"}}',
        f'{{"token_secret_name":"{FAKE_GITHUB_TOKEN}"}}',
        f'{{"auth":{{"token_secret_arn":"{FAKE_GITLAB_TOKEN}"}}}}',
        '{"token_secret_name":"connectors/github/token"}',
        '{"oauth":{"client_secret_name":"connectors/oauth/client"}}',
        '{"secret_arn":"connectors/github/token"}',
    ],
)
def test_validate_credentials_rejects(configuration: str) -> None:
    validator = ConfigurationValidator()

    with pytest.raises(ConfigurationValidationError, match="Secrets Manager references"):
        validator.validate(SecretStr(configuration))


@pytest.mark.parametrize(
    "configuration",
    [
        '{"password_secret_arn":"arn:aws:secretsmanager:us-east-1:123456789012:secret:github/token"}',
        '{"token_secret_name":"arn:aws:secretsmanager:us-east-1:123456789012:secret:github/token"}',
        '{"credential_id":"connectors/github/credential"}',
        '{"auth":{"token_secret_arn":"arn:aws-us-gov:secretsmanager:us-gov-west-1:123456789012:secret:connector/token"}}',
        '{"oauth":{"credential_secret_arn":"arn:aws:secretsmanager:us-east-1:123456789012:secret:oauth/credential"}}',
        '{"authorization":{"password_secret_arn":"arn:aws:secretsmanager:us-east-1:123456789012:secret:authorization/password"}}',
        '{"auth":{"nested":{"api_key_secret_arn":"arn:aws-cn:secretsmanager:cn-north-1:123456789012:secret:connector/api-key"}}}',
        '{"authorization":{"references":[{"token_secret_arn":"arn:aws:secretsmanager:us-east-1:123456789012:secret:authorization/token"},{"password_secret_arn":"arn:aws:secretsmanager:us-west-2:123456789012:secret:authorization/password"}]}}',
        '{"oauth":{"nested":[{"client_secret_arn":"arn:aws:secretsmanager:us-east-1:123456789012:secret:oauth/client"},{"refresh_token_secret_arn":"arn:aws:secretsmanager:us-east-1:123456789012:secret:oauth/refresh"}]}}',
        '{"key_secret_arn":"arn:aws-eusc:secretsmanager:eusc-de-east-1:123456789012:secret:connector/key"}',
    ],
)
def test_validate_secrets_manager_references_accepts(configuration: str) -> None:
    assert ConfigurationValidator().validate(SecretStr(configuration)).get_secret_value() == configuration


def test_validate_benign_fields_with_security_words_accepts() -> None:
    configuration = (
        '{"authentication_mode":"oauth2","password_policy":"rotate","token_count":2,'
        '"keyboard_layout":"us","repository_key":"repo-1",'
        '"headers":{"Content-Type":"application/json","X-Request-Key":"correlation"}}'
    )

    validated = ConfigurationValidator().validate(SecretStr(configuration))

    assert validated.get_secret_value() == configuration


def test_decrypt_wrong_encryption_context_rejects_without_plaintext() -> None:
    plaintext = "customer-private-configuration"
    client = MagicMock()
    client.decrypt.return_value = (
        plaintext.encode(),
        SimpleNamespace(encryption_context={"resource_arn": "arn:aws:ccf:us-east-1:123456789012:wrong"}),
    )
    codec = KmsConfigurationCodec(client, MagicMock())

    with pytest.raises(ConfigurationDecryptionError) as error:
        codec.decrypt(
            DecryptConfigurationRequest(
                b"ciphertext",
                "arn:aws:ccf:us-east-1:123456789012:custom-connector/cc-1",
            )
        )

    assert plaintext not in repr(error.value)
