.PHONY: help install build fix check test requirements clean deploy

help:
	@echo "install      - uv sync (runtime + dev deps)"
	@echo "build        - validate the package imports"
	@echo "check        - lint + type-check + unit tests (non-mutating)"
	@echo "test         - unit tests with coverage"
	@echo "fix          - ruff --fix and format"
	@echo "requirements - freeze runtime deps to src/requirements.txt for Lambda packaging"
	@echo "clean        - remove caches and venv"

install:
	uv sync --extra dev

build:
	CONFIGURATION_KEY_ARN=arn:aws:kms:us-east-1:000000000000:key/00000000-0000-0000-0000-000000000000 \
	INGESTION_BUCKET_NAME=ccf-build-ingestion \
	RUNTIME_ROLE_BOUNDARY_ARN=arn:aws:iam::000000000000:policy/CcfConnectorRuntimeBoundary \
	CONNECTOR_RUNTIME_ROUTES='[{"method":"GET","path":"/api/v1/custom-connectors/{connector_id}"},{"method":"GET","path":"/api/v1/custom-connectors/{connector_id}/jobs/{job_id}"},{"method":"GET","path":"/api/v1/custom-connectors/{connector_id}/checkpoint"},{"method":"PUT","path":"/api/v1/custom-connectors/{connector_id}/checkpoint"},{"method":"DELETE","path":"/api/v1/custom-connectors/{connector_id}/checkpoint"},{"method":"GET","path":"/api/v1/custom-connectors/{connector_id}/documents"},{"method":"POST","path":"/api/v1/custom-connectors/{connector_id}/documents"},{"method":"DELETE","path":"/api/v1/custom-connectors/{connector_id}/documents"}]' \
	ALLOWED_CLIENT_ACCOUNT_IDS=210987654321 \
	AWS_EC2_METADATA_DISABLED=true PYTHONPATH=src uv run python -c "import api_handler, job_orchestrator_handler, job_status_handler, ccf_api_models; print('OK')"

check:
	uv run ruff check src tests
	uv run mypy --explicit-package-bases src
	uv run pytest tests/unit -q

test:
	uv run coverage run -m pytest tests/unit -q
	uv run coverage report

fix:
	uv run ruff check --fix src tests
	uv run ruff format src tests

requirements:
	uv pip compile pyproject.toml -o src/requirements.txt --no-emit-package ccf-api-models

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage htmlcov .venv

deploy:
	@echo "deploy: backend is deployed as a container image by the cdk/ project"
