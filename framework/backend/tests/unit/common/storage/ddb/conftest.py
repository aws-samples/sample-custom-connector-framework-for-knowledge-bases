"""The DAO tests' world over the moto tables, and the spy that records consistent reads."""

from __future__ import annotations

from typing import Any

import pytest

from common.resource_data_codec import ResourceDataCodec
from common.settings import TableNames
from tests.unit.dao_world import ConsistentReadSpy, DaoTables, DaoWorld


@pytest.fixture
def world(dynamodb: Any, table_names: TableNames, codec: ResourceDataCodec) -> DaoWorld:
    return DaoWorld(DaoTables(resource=dynamodb, names=table_names, codec=codec))


@pytest.fixture
def consistent_read_spy() -> type[ConsistentReadSpy]:
    return ConsistentReadSpy
