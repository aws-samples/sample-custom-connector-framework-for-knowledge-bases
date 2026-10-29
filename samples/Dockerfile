# Build from the repository root: docker build -f samples/Dockerfile .
# The service can override the default connector definition at job submission.
# The base images track their latest releases so each build picks up security
# patches. Python stays on 3.14 to match the backend Lambda runtime.
ARG UV_IMAGE=ghcr.io/astral-sh/uv:latest
ARG PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.14-slim

FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS builder

COPY --from=uv /uv /bin/uv
WORKDIR /build
COPY api/packages/ccf-api-models /build/api/packages/ccf-api-models
COPY toolkit /build/toolkit
COPY samples /build/samples
ENV UV_PROJECT_ENVIRONMENT=/opt/connector
WORKDIR /build/samples
RUN uv sync --frozen --no-dev --no-editable

FROM ${PYTHON_IMAGE}
RUN useradd --create-home --uid 10001 connector
COPY --from=builder /opt/connector /opt/connector
ENV PATH="/opt/connector/bin:${PATH}"
WORKDIR /home/connector
USER connector
CMD ["ccf-connector", "run", "web_crawler.connector:definition"]
