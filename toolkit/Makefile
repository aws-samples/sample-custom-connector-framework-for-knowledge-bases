.PHONY: help install generated-client build fix check test clean deploy

GENERATED_CLIENT := src/custom_connector_framework/_generated_client.py
CLIENT_GENERATOR := ../api/codegen/generate_client.py
OPENAPI := ../api/generated/openapi.json

help:
	@echo "install - uv sync (installs ccf-api-models from ../api)"
	@echo "build   - no compiled artifact; validates the package imports"
	@echo "check   - lint + type-check + unit tests (non-mutating)"
	@echo "test    - unit tests with coverage"
	@echo "fix     - ruff --fix and format"
	@echo "clean   - remove caches and venv"

install:
	uv sync --extra dev

generated-client:
	@if [ ! -f $(GENERATED_CLIENT) ]; then \
		if [ ! -f $(OPENAPI) ]; then $(MAKE) --no-print-directory -C ../api contract; fi; \
		uv run python $(CLIENT_GENERATOR) --openapi $(OPENAPI) --output $(GENERATED_CLIENT); \
	fi

build: generated-client
	uv run python -c "import custom_connector_framework; print('OK')"

check: generated-client
	uv run ruff check src tests
	uv run mypy src tests/test_client.py
	uv run pytest tests/ -q

test: generated-client
	uv run coverage run -m pytest tests/ -q
	uv run coverage report

fix:
	uv run ruff check --fix src tests
	uv run ruff format src tests

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage htmlcov .venv
	rm -f $(GENERATED_CLIENT)

deploy:
	@echo "deploy: toolkit has no deployable; it is installed by backend, samples, and connector containers"
