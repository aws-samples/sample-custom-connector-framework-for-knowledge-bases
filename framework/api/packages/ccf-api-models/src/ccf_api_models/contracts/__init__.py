"""Typed readers of the contracts the Python and TypeScript projects share.

Each JSON file beside this module is one contract, read by one `Contract` subclass. The CDK reads
the same files, so a table, metric, or environment variable name exists once. Lambda sets
`AWS_REGION` itself, so no handler's settings list holds it.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from importlib.resources import files
from typing import ClassVar, Self

from pydantic import ConfigDict
from pydantic.alias_generators import to_camel

from ccf_api_models.base import ValueModel


class _ContractValue(ValueModel):
    """A value read from a contract file, whose keys are camelCase for the TypeScript readers."""

    model_config = ConfigDict(alias_generator=to_camel)


class Contract(_ContractValue):
    """A contract file and the model that reads it."""

    file_name: ClassVar[str]

    @classmethod
    def load(cls) -> Self:
        """Read and validate the packaged contract file; an unknown key fails."""
        return cls.model_validate_json(files(__name__).joinpath(cls.file_name).read_text(encoding="utf-8"))


class AttributeType(StrEnum):
    STRING = "STRING"
    NUMBER = "NUMBER"


class KeyAttribute(_ContractValue):
    name: str
    type: AttributeType


class IndexContract(_ContractValue):
    partition_key: KeyAttribute
    sort_key: KeyAttribute | None
    projection: str
    """The generated summary schema whose members the index holds besides the keys."""


class TableContract(_ContractValue):
    name: str
    partition_key: KeyAttribute
    sort_key: KeyAttribute | None
    binary_attributes: frozenset[str]
    """Attributes written as DynamoDB binary: the ciphertext of encrypted members."""
    time_to_live_attribute: str | None
    indexes: Mapping[str, IndexContract]


class TablesContract(Contract):
    file_name: ClassVar[str] = "tables.json"

    destinations: TableContract
    connectors: TableContract
    jobs: TableContract
    collections: TableContract
    documents: TableContract
    idempotency: TableContract


class MetricUnitName(StrEnum):
    COUNT = "Count"
    SECONDS = "Seconds"


class MetricContract(_ContractValue):
    name: str
    unit: MetricUnitName
    dimension_sets: tuple[tuple[str, ...], ...]
    """Each set is the `dimensions` keys one published copy of the metric carries."""


class MetricsContract(Contract):
    """The server-side metrics; `codegen/write_enums.py` writes `MetricName` and `MetricDimensionName` from it."""

    file_name: ClassVar[str] = "metrics.json"

    namespace: str
    dimensions: Mapping[str, str]
    metrics: Mapping[str, MetricContract]


class SettingsContract(Contract):
    """The environment variable names the CDK writes for each Lambda handler module."""

    file_name: ClassVar[str] = "settings.json"

    handlers: Mapping[str, frozenset[str]]


class RuntimeRoleTagsContract(Contract):
    """The tag keys on a runtime role, which the backend writes and the runtime role boundary's policy reads."""

    file_name: ClassVar[str] = "runtime_role_tags.json"

    destination_id: str
    connector_id: str
    tenant_account: str
    source_access_role: str
    log_delivery_role: str


class JobContainers(_ContractValue):
    connector: str
    log_router: str


class JobContainerContract(Contract):
    """A job's two containers, the variables the connector container reads, and the exit codes it ends with.

    `codegen/write_enums.py` writes `RuntimeVariable` and `JobExitCode` in `job_container.py` from it.
    """

    file_name: ClassVar[str] = "job_container.json"

    containers: JobContainers
    variables: Mapping[str, str]
    """camelCase key to environment variable name."""
    exit_codes: Mapping[str, int]
    """Exit code name to the code."""


class TenantExample(_ContractValue):
    partition: str
    region: str
    account_id: str
    arn_prefix: str


class IdentifierExamples(Contract):
    """Literal IDs, ARNs, and keys the Python and TypeScript tests of the identity model and key layout expect."""

    file_name: ClassVar[str] = "identifier_examples.json"

    tenants: tuple[TenantExample, ...]
    destination_id: str
    destination_arn: str
    connector_id: str
    connector_arn: str
    destination_connector_key: str
    connector_key_prefix: str
    job_id: str
    job_arn: str
    source_collection_id: str
    collection_id: str
    source_document_id: str
    document_id: str
    document_arn: str
    document_prefix: str
    document_object_key: str
    key_outside_the_layout: str
