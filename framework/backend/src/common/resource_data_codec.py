"""Envelope encryption of the resource data CCF stores for a tenant: connector configurations, job
configurations, and checkpoints.

CCF never parses a value: any string within its byte limit is encrypted with the resource's ARN
as encryption context, stored, and returned unchanged. A decrypt passes the same context, so the
SDK refuses a ciphertext that belongs to another resource.
"""

from __future__ import annotations

from typing import Protocol, cast, runtime_checkable

import boto3
from aws_cryptographic_material_providers.mpl import (  # type: ignore[import-untyped]
    AwsCryptographicMaterialProviders,
)
from aws_cryptographic_material_providers.mpl.config import MaterialProvidersConfig  # type: ignore[import-untyped]
from aws_cryptographic_material_providers.mpl.models import CreateAwsKmsKeyringInput  # type: ignore[import-untyped]
from aws_cryptographic_material_providers.mpl.references import IKeyring  # type: ignore[import-untyped]
from aws_encryption_sdk import CommitmentPolicy, EncryptionSDKClient  # type: ignore[import-untyped]
from ccf_api_models.base import ValueModel
from pydantic import SecretStr

from common.errors import InternalServiceError

_RESOURCE_ARN_CONTEXT_KEY = "resource_arn"


class EncryptResourceDataRequest(ValueModel):
    data: SecretStr
    resource_arn: str


class DecryptResourceDataRequest(ValueModel):
    ciphertext: bytes
    resource_arn: str


class ResourceDataDecryptionError(InternalServiceError):
    """The ciphertext doesn't belong to the resource, or the SDK couldn't decrypt it."""


@runtime_checkable
class ResourceDataCodec(Protocol):
    """Runtime-checkable, so a Pydantic model can hold one."""

    def encrypt(self, request: EncryptResourceDataRequest) -> bytes: ...

    def decrypt(self, request: DecryptResourceDataRequest) -> SecretStr: ...


class KmsResourceDataCodec:
    def __init__(self, client: EncryptionSDKClient, keyring: IKeyring) -> None:
        self._client = client
        self._keyring = keyring

    @classmethod
    def for_key(cls, key_arn: str) -> KmsResourceDataCodec:
        """Build the KMS keyring and the commitment-enforcing SDK client once, at a composition root."""
        keyring = AwsCryptographicMaterialProviders(config=MaterialProvidersConfig()).create_aws_kms_keyring(
            input=CreateAwsKmsKeyringInput(kms_key_id=key_arn, kms_client=boto3.client("kms"))
        )
        return cls(EncryptionSDKClient(commitment_policy=CommitmentPolicy.REQUIRE_ENCRYPT_REQUIRE_DECRYPT), keyring)

    def encrypt(self, request: EncryptResourceDataRequest) -> bytes:
        ciphertext, _ = self._client.encrypt(
            source=request.data.get_secret_value().encode("utf-8"),
            keyring=self._keyring,
            encryption_context={_RESOURCE_ARN_CONTEXT_KEY: request.resource_arn},
        )
        return cast(bytes, ciphertext)

    def decrypt(self, request: DecryptResourceDataRequest) -> SecretStr:
        try:
            plaintext, _ = self._client.decrypt(
                source=request.ciphertext,
                keyring=self._keyring,
                encryption_context={_RESOURCE_ARN_CONTEXT_KEY: request.resource_arn},
            )
            return SecretStr(cast(bytes, plaintext).decode("utf-8"))
        except Exception as error:
            raise ResourceDataDecryptionError(f"Data of {request.resource_arn} could not be decrypted") from error
