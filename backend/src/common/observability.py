"""Structured, customer-content-free observability fields."""

from dataclasses import asdict, dataclass

from aws_lambda_powertools import Logger, Tracer

logger = Logger(service="CustomConnectorFramework")
tracer = Tracer(service="CustomConnectorFramework")


@dataclass(frozen=True)
class LogContext:
    """Low-cardinality identifiers safe for structured logs."""

    connector_id: str | None = None
    account_id: str | None = None
    job_id: str | None = None


def create_log_context(context: LogContext) -> dict[str, str]:
    """Remove absent fields from a structured log context."""
    return {key: value for key, value in asdict(context).items() if value is not None}
