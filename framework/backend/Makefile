.PHONY: install build fix check test clean deploy

# uv can pick an x86_64 Python on Apple Silicon, which forces native wheels to build from source.
ifeq ($(shell uname -sm),Darwin arm64)
UV_VENV_PYTHON := --python cpython-3.14-macos-aarch64-none
endif


install:
	uv venv --allow-existing --quiet $(UV_VENV_PYTHON)
	uv sync --locked

# Handlers read their settings from the environment at import, so the unit tests import them;
# this target imports every other module and writes the ingestion message's contract file.
INGESTION_SCHEMA := generated/ingestion-request-message.schema.json
PRINT_INGESTION_SCHEMA := PYTHONPATH=src uv run --locked python -m common.ingestion_requests

build:
	PYTHONPATH=src uv run --locked python -c "import importlib, pathlib; [importlib.import_module('.'.join(path.relative_to('src').with_suffix('').parts)) for path in sorted(pathlib.Path('src').rglob('*.py')) if not path.name.endswith('_handler.py') and path.name != '__init__.py']; print('OK')"
	$(PRINT_INGESTION_SCHEMA) > $(INGESTION_SCHEMA)

# The tests read the API's generated OpenAPI document, which framework/api's build writes.
API_OPENAPI := ../api/generated/openapi.json
$(API_OPENAPI):
	@echo "$(API_OPENAPI) is missing: run make -C ../api build first." >&2; exit 1

check: $(API_OPENAPI)
	uv run --locked ruff check src tests
	uv run --locked ruff format --check src tests
	uv run --locked mypy --explicit-package-bases src tests
	$(PRINT_INGESTION_SCHEMA) | diff -u $(INGESTION_SCHEMA) - || { echo "$(INGESTION_SCHEMA) is stale: run make build." >&2; exit 1; }
	uv run --locked pytest tests/unit -q

test: $(API_OPENAPI)
	uv run --locked coverage run -m pytest tests/unit -q
	uv run --locked coverage report

fix:
	uv run --locked ruff check --fix src tests
	uv run --locked ruff format src tests

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage htmlcov .venv

deploy:
	@echo "deploy: framework/cdk bundles the backend into its Lambda functions; run make -C ../cdk deploy"
