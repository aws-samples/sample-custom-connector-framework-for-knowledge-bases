# Build from the repository root, which holds the toolkit and API models this image installs:
# finch build --platform linux/arm64 -f connectors/github/backend/Dockerfile .
# The base images are pinned to version tags, not digests, so raise the tags to pick up security patches.
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.7
ARG PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.14.10-slim

FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS builder

COPY --from=uv /uv /bin/uv
WORKDIR /build
COPY framework/api/packages/ccf-api-models /build/framework/api/packages/ccf-api-models
COPY framework/toolkit /build/framework/toolkit
COPY connectors/github/backend/pyproject.toml connectors/github/backend/uv.lock /build/connectors/github/backend/
ENV UV_PROJECT_ENVIRONMENT=/opt/connector
WORKDIR /build/connectors/github/backend
RUN uv sync --frozen --no-dev --no-editable --no-install-project

FROM ${PYTHON_IMAGE}
RUN useradd --create-home --uid 10001 connector
COPY --from=builder /opt/connector /opt/connector
COPY connectors/github/backend/src /opt/app
ENV PATH="/opt/connector/bin:${PATH}" PYTHONPATH="/opt/app"
WORKDIR /home/connector
USER connector
# The custom connector's command names the implementation: github_connector:GitHubConnectorImplementation.
ENTRYPOINT ["ccf-connector", "run"]
