"""Validation and envelope encryption for connector configuration."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol, cast

from aws_cryptographic_material_providers.mpl.references import IKeyring  # type: ignore[import-untyped]
from aws_encryption_sdk import CommitmentPolicy, EncryptionSDKClient  # type: ignore[import-untyped]
from pydantic import SecretStr

MAX_CONFIGURATION_BYTES = 65_536
_RESOURCE_ARN_CONTEXT_KEY = "resource_arn"
_SENSITIVE_FIELD = re.compile(
    r"^(?:auth|authorization|oauth|key|keys|credential|credentials|password|passwords|passwd|secret|secrets|"
    r"token|tokens|access_key|access_key_id|secret_access_key|api_key|client_secret|private_key|refresh_token|"
    r"access_token|auth_header|auth_headers|authorization_header|authorization_headers)$|"
    r"(?:^|_)(?:access_key|api_key|client_secret|private_key|refresh_token|access_token|password|passwd|secret|token|credential)$|"
    r"^(?:access_key|api_key|client_secret|private_key|refresh_token|access_token|password|passwd|secret|token|"
    r"credential|key)_value$|^value_(?:access_key|api_key|client_secret|private_key|refresh_token|access_token|"
    r"password|passwd|secret|token|credential|key)$",
    re.IGNORECASE,
)
_CREDENTIAL_CONTAINER_FIELDS = frozenset({"auth", "authorization", "oauth"})
_HEADER_CONTAINER_FIELDS = frozenset({"header", "headers", "auth_header", "auth_headers"})
_SECRET_REFERENCE_FIELD = re.compile(
    r"(^|_)(secret|credential).*(arn|id|name|ref|reference)$|(^|_)(arn|id|name|ref|reference).*(secret|credential)$",
    re.IGNORECASE,
)
_SECRET_WORD_FIELD = re.compile(r"(^|_)secret(_|$)", re.IGNORECASE)
_SECRET_ARN = re.compile(
    r"^arn:(aws|aws-us-gov|aws-cn|aws-eusc):secretsmanager:(?:[a-z]{2}|eusc)(?:-[a-z]+)+-[0-9]+:"
    r"[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+$"
)
_SECRET_NAME = re.compile(r"^[A-Za-z0-9/_+=.@-]{1,512}$")
_OBVIOUS_CREDENTIAL = re.compile(
    r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|"
    r"\bgh[opusr]_[A-Za-z0-9_]{20,}\b|"
    r"\bglpat-[A-Za-z0-9_-]{20,}\b|"
    r"\bBearer\s+[A-Za-z0-9._~-]{12,})",
    re.IGNORECASE,
)


class ConfigurationValidationError(ValueError):
    """Raised when configuration cannot be safely persisted."""


class ConfigurationDecryptionError(ValueError):
    """Raised when ciphertext identity or contents cannot be verified."""


@dataclass(frozen=True)
class EncryptConfigurationRequest:
    """Plaintext and non-secret resource identity for encryption."""

    configuration: SecretStr
    resource_arn: str


@dataclass(frozen=True)
class DecryptConfigurationRequest:
    """Ciphertext and expected non-secret resource identity for decryption."""

    ciphertext: bytes
    resource_arn: str


class ConfigurationCodec(Protocol):
    """Envelope-encryption boundary for configuration JSON."""

    def encrypt(self, request: EncryptConfigurationRequest) -> bytes:
        """Encrypt validated UTF-8 JSON."""

    def decrypt(self, request: DecryptConfigurationRequest) -> SecretStr:
        """Decrypt UTF-8 JSON after verifying resource identity."""


class ConfigurationValidator:
    """Validates JSON shape, byte size, and secret-reference use."""

    def validate(self, configuration: SecretStr) -> SecretStr:
        """Return validated configuration without exposing it in representations."""
        plaintext = configuration.get_secret_value()
        if len(plaintext.encode("utf-8")) > MAX_CONFIGURATION_BYTES:
            raise ConfigurationValidationError("Configuration exceeds the 65,536-byte UTF-8 limit")
        try:
            parsed: Any = json.loads(plaintext)
        except json.JSONDecodeError as error:
            raise ConfigurationValidationError("Configuration must be valid JSON") from error
        if not isinstance(parsed, dict):
            raise ConfigurationValidationError("Configuration must be a JSON object")
        self._reject_credentials(parsed)
        return configuration

    def _reject_credentials(
        self,
        value: Any,
        *,
        inside_headers: bool = False,
        inside_credential_container: bool = False,
    ) -> None:
        if isinstance(value, dict):
            self._reject_mapping(value, inside_headers, inside_credential_container)
            return
        if isinstance(value, list):
            self._reject_sequence(value, inside_headers, inside_credential_container)
            return
        if inside_credential_container or isinstance(value, str) and _OBVIOUS_CREDENTIAL.search(value):
            raise self._credential_reference_error()

    def _reject_mapping(
        self,
        values: dict[Any, Any],
        inside_headers: bool,
        inside_credential_container: bool,
    ) -> None:
        if inside_credential_container and not values:
            raise self._credential_reference_error()
        for field_name, nested_value in values.items():
            normalized_name = self._normalize_field_name(str(field_name))
            if isinstance(nested_value, str) and _OBVIOUS_CREDENTIAL.search(nested_value):
                raise self._credential_reference_error()
            if _SECRET_REFERENCE_FIELD.search(normalized_name) is not None:
                if not self._is_secret_reference(normalized_name, nested_value):
                    raise self._credential_reference_error()
                continue
            nested_is_container = isinstance(nested_value, (dict, list))
            field_is_credential_container = normalized_name in _CREDENTIAL_CONTAINER_FIELDS or (
                nested_is_container and _SENSITIVE_FIELD.search(normalized_name) is not None
            )
            if inside_credential_container and not nested_is_container:
                raise self._credential_reference_error()
            if self._is_disallowed_field(normalized_name, nested_value, inside_headers=inside_headers):
                raise self._credential_reference_error()
            self._reject_credentials(
                nested_value,
                inside_headers=inside_headers or normalized_name in _HEADER_CONTAINER_FIELDS,
                inside_credential_container=inside_credential_container or field_is_credential_container,
            )

    def _reject_sequence(
        self,
        values: list[Any],
        inside_headers: bool,
        inside_credential_container: bool,
    ) -> None:
        if inside_credential_container and not values:
            raise self._credential_reference_error()
        for nested_value in values:
            if inside_credential_container and not isinstance(nested_value, (dict, list)):
                raise self._credential_reference_error()
            self._reject_credentials(
                nested_value,
                inside_headers=inside_headers,
                inside_credential_container=inside_credential_container,
            )

    @staticmethod
    def _credential_reference_error() -> ConfigurationValidationError:
        return ConfigurationValidationError(
            "Configuration must contain Secrets Manager references rather than credentials"
        )

    @staticmethod
    def _is_disallowed_field(field_name: str, value: Any, *, inside_headers: bool) -> bool:
        if field_name in _HEADER_CONTAINER_FIELDS:
            return not isinstance(value, (dict, list))
        if isinstance(value, (dict, list)) and (
            field_name in _CREDENTIAL_CONTAINER_FIELDS or _SENSITIVE_FIELD.search(field_name) is not None
        ):
            return False
        if inside_headers and field_name in {"auth", "authorization", "oauth", "key", "token"}:
            return True
        return _SENSITIVE_FIELD.search(field_name) is not None

    @staticmethod
    def _is_secret_reference(field_name: str, value: Any) -> bool:
        if not isinstance(value, str):
            return False
        if _SECRET_ARN.fullmatch(value) is not None:
            return True
        if _SECRET_WORD_FIELD.search(field_name) is not None:
            return False
        return _SECRET_NAME.fullmatch(value) is not None

    @staticmethod
    def _normalize_field_name(field_name: str) -> str:
        with_word_boundaries = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", field_name)
        return re.sub(r"[^A-Za-z0-9]+", "_", with_word_boundaries).strip("_").lower()


class KmsConfigurationCodec:
    """AWS Encryption SDK v4 keyring implementation of configuration encryption."""

    def __init__(self, client: EncryptionSDKClient, keyring: IKeyring) -> None:
        self._client = client
        self._keyring = keyring

    def encrypt(self, request: EncryptConfigurationRequest) -> bytes:
        """Encrypt configuration with resource identity as authenticated context."""
        ciphertext, _ = self._client.encrypt(
            source=request.configuration.get_secret_value().encode("utf-8"),
            keyring=self._keyring,
            encryption_context=self._context(request.resource_arn),
        )
        return cast(bytes, ciphertext)

    def decrypt(self, request: DecryptConfigurationRequest) -> SecretStr:
        """Decrypt only when the message header contains the expected identity."""
        expected_context = self._context(request.resource_arn)
        try:
            plaintext, header = self._client.decrypt(
                source=request.ciphertext,
                keyring=self._keyring,
                encryption_context=expected_context,
            )
        except Exception as error:
            raise ConfigurationDecryptionError("Configuration could not be decrypted") from error
        if any(header.encryption_context.get(key) != expected for key, expected in expected_context.items()):
            raise ConfigurationDecryptionError("Configuration encryption context does not match the resource")
        try:
            return SecretStr(cast(bytes, plaintext).decode("utf-8"))
        except UnicodeDecodeError as error:
            raise ConfigurationDecryptionError("Configuration is not valid UTF-8") from error

    @staticmethod
    def _context(resource_arn: str) -> dict[str, str]:
        return {_RESOURCE_ARN_CONTEXT_KEY: resource_arn}


def create_encryption_client() -> EncryptionSDKClient:
    """Create the commitment-enforcing SDK client at the composition root."""
    return EncryptionSDKClient(
        commitment_policy=CommitmentPolicy.REQUIRE_ENCRYPT_REQUIRE_DECRYPT,
    )
