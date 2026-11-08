"""Collection and document IDs are the hashes of their source IDs."""

from __future__ import annotations

import json

import pytest
from ccf_api_models.contracts import IdentifierExamples
from ccf_api_models.models import BatchPutCustomConnectorDocumentsRequest, PutCustomConnectorCheckpointRequest

from common.errors import BadRequestError
from common.validators.source_identifiers import CollectionIdentifierValidator, DocumentIdentifierValidator

EXAMPLES = IdentifierExamples.load()
_DOCUMENT = {
    "document_id": EXAMPLES.document_id,
    "source_document_id": EXAMPLES.source_document_id,
    "content_hash": "sha256:9f2c",
}


def _batch_put(**members: object) -> BatchPutCustomConnectorDocumentsRequest:
    body = {
        "destination_id": EXAMPLES.destination_id,
        "connector_id": EXAMPLES.connector_id,
        "documents": [_DOCUMENT],
        **members,
    }
    return BatchPutCustomConnectorDocumentsRequest.model_validate_json(json.dumps(body))


def _checkpoint_put(**members: object) -> PutCustomConnectorCheckpointRequest:
    body = {
        "destination_id": EXAMPLES.destination_id,
        "connector_id": EXAMPLES.connector_id,
        "checkpoint_data": "{}",
        **members,
    }
    return PutCustomConnectorCheckpointRequest.model_validate_json(json.dumps(body))


def _rejected_field(request: BatchPutCustomConnectorDocumentsRequest | PutCustomConnectorCheckpointRequest) -> str:
    with pytest.raises(BadRequestError) as raised:
        CollectionIdentifierValidator().validate(request)
    (field,) = raised.value.fields
    return field


def test_a_request_without_a_collection_names_the_default_collection() -> None:
    assert CollectionIdentifierValidator().validate(_checkpoint_put()) == "col-default"


def test_a_collection_id_matching_its_source_id_passes() -> None:
    request = _batch_put(collection_id=EXAMPLES.collection_id, source_collection_id=EXAMPLES.source_collection_id)

    assert CollectionIdentifierValidator().validate(request) == EXAMPLES.collection_id


def test_a_collection_id_that_isnt_its_source_ids_hash_is_rejected() -> None:
    request = _checkpoint_put(collection_id=EXAMPLES.collection_id, source_collection_id="another")

    assert _rejected_field(request) == "source_collection_id"


def test_a_collection_without_a_source_id_is_rejected() -> None:
    assert _rejected_field(_batch_put(collection_id=EXAMPLES.collection_id)) == "source_collection_id"


def test_the_default_collection_with_a_source_id_is_rejected() -> None:
    assert _rejected_field(_batch_put(source_collection_id=EXAMPLES.source_collection_id)) == "source_collection_id"


def test_a_document_id_that_isnt_its_source_ids_hash_is_rejected() -> None:
    request = _batch_put(documents=[{**_DOCUMENT, "source_document_id": "another"}])

    with pytest.raises(BadRequestError) as raised:
        DocumentIdentifierValidator().validate(request)

    assert raised.value.fields == ("document_id",)


def test_repeated_document_ids_are_rejected() -> None:
    with pytest.raises(BadRequestError) as raised:
        DocumentIdentifierValidator().validate(_batch_put(documents=[_DOCUMENT, _DOCUMENT]))

    assert raised.value.fields == ("documents",)
