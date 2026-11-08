"""Tenants, references, ARNs, derived IDs, and the key layout match the literal examples in the contract."""

import pytest
from ccf_api_models.contracts import IdentifierExamples, TenantExample
from ccf_api_models.identifiers import (
    CollectionIdentity,
    CollectionRef,
    ConnectorRef,
    DestinationRef,
    DocumentKey,
    DocumentRef,
    JobRef,
    Partition,
    SourceIdKind,
    TenantContext,
)

EXAMPLES = IdentifierExamples.load()
TENANT = TenantContext.from_arn_prefix(EXAMPLES.tenants[0].arn_prefix)
DESTINATION = DestinationRef(tenant=TENANT, destination_id=EXAMPLES.destination_id)
CONNECTOR = ConnectorRef(destination=DESTINATION, connector_id=EXAMPLES.connector_id)
DOCUMENT = DocumentRef(
    collection=CollectionRef(connector=CONNECTOR, collection_id=EXAMPLES.collection_id),
    document_id=EXAMPLES.document_id,
)


@pytest.mark.parametrize("example", EXAMPLES.tenants, ids=lambda example: example.partition)
def test_a_tenant_arn_prefix_round_trips_in_each_partition(example: TenantExample) -> None:
    tenant = TenantContext(partition=Partition(example.partition), region=example.region, account_id=example.account_id)

    assert (tenant.arn_prefix, TenantContext.from_arn_prefix(example.arn_prefix)) == (example.arn_prefix, tenant)


@pytest.mark.parametrize(
    "arn_prefix", ["arn:aws:ccf:us-east-1:11112222333", "arn:aws-iso:ccf:us-east-1:111122223333", "arn:aws:ccf::1"]
)
def test_a_malformed_tenant_arn_prefix_is_rejected(arn_prefix: str) -> None:
    with pytest.raises(ValueError, match="tenant ARN prefix is invalid"):
        TenantContext.from_arn_prefix(arn_prefix)


def test_each_resource_arn_extends_its_parents() -> None:
    job = JobRef(connector=CONNECTOR, job_id=EXAMPLES.job_id)

    assert (DESTINATION.arn, CONNECTOR.arn, job.arn, DOCUMENT.arn) == (
        EXAMPLES.destination_arn,
        EXAMPLES.connector_arn,
        EXAMPLES.job_arn,
        EXAMPLES.document_arn,
    )


def test_a_connector_names_its_table_key_and_s3_prefix() -> None:
    assert (CONNECTOR.destination_connector_key, CONNECTOR.key_prefix) == (
        EXAMPLES.destination_connector_key,
        EXAMPLES.connector_key_prefix,
    )


def test_a_connectors_schedule_name_is_ccf_and_32_hex_of_its_arns_sha256() -> None:
    assert CONNECTOR.schedule_name == "ccf-dd5e78f2352a3c693473f113b2d30094"


def test_a_document_arn_round_trips() -> None:
    assert DocumentRef.from_arn(EXAMPLES.document_arn) == DOCUMENT


def test_an_arn_that_isnt_a_ccf_document_arn_parses_to_none() -> None:
    assert DocumentRef.from_arn(EXAMPLES.connector_arn) is None


def test_a_documents_key_is_its_labeled_prefix() -> None:
    assert DOCUMENT.key.prefix == EXAMPLES.document_prefix


def test_an_object_key_inside_the_layout_names_its_document() -> None:
    assert DocumentKey.of_object_key(EXAMPLES.document_object_key) == DOCUMENT.key


def test_an_object_key_outside_the_layout_names_no_document() -> None:
    assert DocumentKey.of_object_key(EXAMPLES.key_outside_the_layout) is None


def test_a_source_document_id_derives_its_document_id() -> None:
    assert SourceIdKind.DOCUMENT.of(EXAMPLES.source_document_id) == EXAMPLES.document_id


def test_no_source_collection_id_is_the_default_collection() -> None:
    assert CollectionIdentity.of(None) == CollectionIdentity(collection_id="col-default", source_collection_id=None)


def test_a_source_collection_id_derives_its_collection_id() -> None:
    assert CollectionIdentity.of(EXAMPLES.source_collection_id).collection_id == EXAMPLES.collection_id
