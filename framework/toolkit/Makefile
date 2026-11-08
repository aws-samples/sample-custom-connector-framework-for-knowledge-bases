.PHONY: install build fix check test clean deploy

# uv can pick an x86_64 Python on Apple Silicon, which forces native wheels to build from source.
ifeq ($(shell uname -sm),Darwin arm64)
UV_VENV_PYTHON := --python cpython-3.14-macos-aarch64-none
endif


install:
	uv venv --allow-existing --quiet $(UV_VENV_PYTHON)
	uv sync --locked

build:
	uv run --locked python -c "import custom_connector_framework; print('OK')"

check:
	uv run --locked ruff check src tests
	uv run --locked ruff format --check src tests
	uv run --locked mypy src tests
	uv run --locked pytest tests/ -q

test:
	uv run --locked coverage run -m pytest tests/ -q
	uv run --locked coverage report

fix:
	uv run --locked ruff check --fix src tests
	uv run --locked ruff format src tests

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage htmlcov .venv

deploy:
	@echo "deploy: the toolkit has no deployable; connector images and the CDK custom resource Lambdas install it"
