"""`DataStores.build` wires every DAO over the root's tables without calling AWS."""

from __future__ import annotations

from typing import Any

import pytest
from ccf_api_models.identifiers import DestinationRef, Partition, TenantContext

from common.composition import DataStores, DataStoresRequest
from common.errors import ResourceNotFoundError
from common.settings import TableNames

TENANT = TenantContext(partition=Partition.AWS, region="us-east-1", account_id="111122223333")


def test_build_makes_no_aws_call(table_names: TableNames, codec: Any) -> None:
    # Outside moto, with no endpoint reachable: building the DAOs must not touch the network.
    stores = DataStores.build(DataStoresRequest(tables=table_names, codec=codec))

    assert set(DataStores.model_fields) == {"destinations", "connectors", "collections", "documents"}
    assert all(getattr(stores, name) is not None for name in DataStores.model_fields)


def test_the_built_daos_read_the_tables_the_settings_name(table_names: TableNames, codec: Any, dynamodb: Any) -> None:
    stores = DataStores.build(DataStoresRequest(tables=table_names, codec=codec))
    missing = DestinationRef(tenant=TENANT, destination_id="dst-7c41e9a2b5d0")

    # A wrong table name fails as an unavailable dependency; the right one finds no item.
    with pytest.raises(ResourceNotFoundError):
        stores.destinations.get(missing)
