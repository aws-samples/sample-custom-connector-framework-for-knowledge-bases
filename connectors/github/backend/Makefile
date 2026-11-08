.PHONY: install build fix check test clean deploy

# uv can pick an x86_64 Python on Apple Silicon, which forces native wheels to build from source.
ifeq ($(shell uname -sm),Darwin arm64)
UV_VENV_PYTHON := --python cpython-3.14-macos-aarch64-none
endif

# The connector configuration's JSON Schema, which the CDK app validates every owner's configuration against.
SCHEMA_FILE := generated/connector-configuration.schema.json
WRITE_SCHEMA := PYTHONPATH=src uv run --locked python -c "import json; \
from common.configuration import GitHubConnectorConfiguration as model; \
print(json.dumps(model.model_json_schema(), indent=2, sort_keys=True))"

install:
	uv venv --allow-existing --quiet $(UV_VENV_PYTHON)
	uv sync --locked

build:
	mkdir -p generated
	$(WRITE_SCHEMA) > $(SCHEMA_FILE)

# A stale schema fails check instead of being rewritten.
check:
	$(WRITE_SCHEMA) | diff -u $(SCHEMA_FILE) -
	uv run --locked ruff check src tests
	uv run --locked ruff format --check src tests
	uv run --locked mypy src tests
	uv run --locked pytest -q

test:
	uv run --locked pytest -q

fix:
	uv run --locked ruff check --fix src tests
	uv run --locked ruff format src tests

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache .venv

deploy:
	@echo "deploy: the cdk app builds this connector's image and registers it with CCF"
