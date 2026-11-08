"""The codec sends each value with its resource's ARN as encryption context, both ways."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr

from common.resource_data_codec import (
    DecryptResourceDataRequest,
    EncryptResourceDataRequest,
    KmsResourceDataCodec,
    ResourceDataDecryptionError,
)

CONNECTOR_ARN = "arn:aws:ccf:us-east-1:111122223333:destination/dst-7c41e9a2b5d0/custom-connector/cc-e020d65d76c0"


def test_encrypt_sends_the_value_with_the_keyring_and_the_resource_arn_as_context() -> None:
    client = MagicMock()
    client.encrypt.return_value = (b"ciphertext", SimpleNamespace())
    keyring = MagicMock()

    ciphertext = KmsResourceDataCodec(client, keyring).encrypt(
        EncryptResourceDataRequest(data=SecretStr('{"org":"awslabs"}'), resource_arn=CONNECTOR_ARN)
    )

    assert ciphertext == b"ciphertext"
    client.encrypt.assert_called_once_with(
        source=b'{"org":"awslabs"}', keyring=keyring, encryption_context={"resource_arn": CONNECTOR_ARN}
    )


def test_decrypt_passes_the_resource_arn_as_the_expected_context_and_returns_the_value() -> None:
    client = MagicMock()
    client.decrypt.return_value = (b'{"org":"awslabs"}', SimpleNamespace())
    keyring = MagicMock()

    value = KmsResourceDataCodec(client, keyring).decrypt(
        DecryptResourceDataRequest(ciphertext=b"ciphertext", resource_arn=CONNECTOR_ARN)
    )

    assert value.get_secret_value() == '{"org":"awslabs"}'
    client.decrypt.assert_called_once_with(
        source=b"ciphertext", keyring=keyring, encryption_context={"resource_arn": CONNECTOR_ARN}
    )


def test_a_failed_decrypt_names_the_resource_and_never_the_ciphertext() -> None:
    client = MagicMock()
    client.decrypt.side_effect = RuntimeError("KMS unavailable")

    with pytest.raises(ResourceDataDecryptionError) as error:
        KmsResourceDataCodec(client, MagicMock()).decrypt(
            DecryptResourceDataRequest(ciphertext=b"ciphertext-bytes", resource_arn=CONNECTOR_ARN)
        )

    assert CONNECTOR_ARN in str(error.value)
    assert "ciphertext-bytes" not in str(error.value)
