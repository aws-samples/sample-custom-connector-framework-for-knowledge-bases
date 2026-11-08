"""A metric is published in the contract's namespace with exactly its own dimensions."""

from __future__ import annotations

import json

import pytest
from ccf_api_models.contracts import MetricsContract

from common.observability import LogContext, MetricDimensionName, MetricName, MetricRecord, ServiceMetrics


def test_a_metric_is_published_in_the_contract_namespace_with_only_its_own_dimensions(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ServiceMetrics().record(
        MetricRecord(
            name=MetricName.DOCUMENTS_RECORDED,
            value=3,
            dimensions={MetricDimensionName.CONNECTOR_ID: "cc-e020d65d76c0"},
        )
    )

    document = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    directive = document["_aws"]["CloudWatchMetrics"][0]
    assert directive["Namespace"] == MetricsContract.load().namespace
    assert directive["Dimensions"] == [["ConnectorId"]]
    assert document["DocumentsRecorded"] == [3.0]


def test_as_log_extra_returns_only_the_present_identifiers() -> None:
    assert LogContext(connector_id="cc-e020d65d76c0").as_log_extra() == {"connector_id": "cc-e020d65d76c0"}
