"""Structured logs, traces, and the framework's server-side metrics.

Every handler shares one logger and tracer. `contracts/metrics.json` lists the metrics and the
dimensions each is published with; the Ops stack's alarms read the same file. Log records and
metric dimensions carry identifiers and counts only, never request or stored values.
"""

from __future__ import annotations

from collections.abc import Mapping

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.metrics import MetricUnit, single_metric
from ccf_api_models.base import ValueModel
from ccf_api_models.metric_names import METRICS_NAMESPACE, MetricDimensionName, MetricName

SERVICE_NAME = "CustomConnectorFramework"

logger = Logger(service=SERVICE_NAME)
tracer = Tracer(service=SERVICE_NAME)

__all__ = ["LogContext", "MetricDimensionName", "MetricName", "MetricRecord", "ServiceMetrics", "logger", "tracer"]


class MetricRecord(ValueModel):
    """One metric value and the dimensions it is published with."""

    name: MetricName
    value: float
    unit: MetricUnit = MetricUnit.Count
    dimensions: Mapping[MetricDimensionName, str] = {}


class ServiceMetrics:
    """Publishes each metric as its own Embedded Metric Format document."""

    def record(self, metric: MetricRecord) -> None:
        """Write one metric with exactly its dimensions."""
        with single_metric(
            name=metric.name.value,
            unit=metric.unit,
            value=metric.value,
            namespace=METRICS_NAMESPACE,
            default_dimensions={},
        ) as published:
            for name, value in metric.dimensions.items():
                published.add_dimension(name=name.value, value=value)


class LogContext(ValueModel):
    """Low-cardinality identifiers safe for structured logs."""

    destination_id: str | None = None
    connector_id: str | None = None
    account_id: str | None = None
    job_id: str | None = None

    def as_log_extra(self) -> dict[str, str]:
        """Return the present fields as log record extras."""
        return self.model_dump(exclude_none=True)
