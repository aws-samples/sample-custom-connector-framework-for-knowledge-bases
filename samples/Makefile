UV_LINK_MODE ?= symlink
export UV_LINK_MODE
# Container CLI for docker-smoke. CDK reads the same variable, so setting
# CDK_DOCKER=finch once selects Finch for both.
CDK_DOCKER ?= docker

.PHONY: help install build smoke-clean docker-smoke check test fix clean

help:
	@echo "install     - install locked runtime and development dependencies with uv"
	@echo "build       - import all connector definitions"
	@echo "smoke-clean - verify definitions and generic launcher in an isolated uv environment"
	@echo "docker-smoke - build the sample image and verify its installed launcher"
	@echo "check       - lint, type-check, and run unit tests"
	@echo "test        - run unit tests with coverage"
	@echo "fix         - apply Ruff fixes and formatting"
	@echo "clean       - remove local caches and the uv environment"

install:
	uv sync --extra dev --locked

build:
	uv run python -c "from github_connector.connector import definition as github; from gitlab_connector.connector import definition as gitlab; from web_crawler.connector import definition as crawler; assert github and gitlab and crawler"

smoke-clean:
# Import definitions and run entry point in same isolated environment, catching packaging drift.
	uv run --isolated --no-project --with ../api/packages/ccf-api-models --with ../toolkit --with . sh -c 'python -I -c "from github_connector.connector import definition as github; from gitlab_connector.connector import definition as gitlab; from web_crawler.connector import definition as crawler; assert github and gitlab and crawler" && ccf-connector --help'

docker-smoke:
	$(CDK_DOCKER) build --pull -f Dockerfile -t ccf-samples-smoke ..
	$(CDK_DOCKER) run --rm ccf-samples-smoke ccf-connector --help

check:
	uv run --extra dev ruff check .
	uv run --extra dev mypy --no-incremental github_connector gitlab_connector web_crawler
	uv run --extra dev pytest -p no:cacheprovider tests/ -q


test:
	uv run --extra dev coverage run -m pytest -p no:cacheprovider tests/ -q
	uv run --extra dev coverage report

fix:
	uv run --extra dev ruff check --fix .
	uv run --extra dev ruff format .

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage htmlcov .venv
