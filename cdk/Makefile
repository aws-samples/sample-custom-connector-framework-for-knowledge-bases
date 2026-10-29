# CDK (TypeScript + npm). `check` is the non-mutating gate; `fix` is its
# mutating counterpart. Verbs mirror backend/ and toolkit/ so the root Makefile
# can fan one verb across every sub-project.

.PHONY: help install build fix check test test-python deploy clean _node_guard api

JEST_ARGS ?=

help:
	@echo "install     - npm dependencies + locked Python dev environment"
	@echo "build   - tsc"
	@echo "check       - typecheck + lint + jest + focused Python tests (non-mutating)"
	@echo "test        - jest + focused Python tests"
	@echo "test-python - focused connector operation Python tests"
	@echo "fix     - prettier + eslint --fix"
	@echo "deploy  - cdk deploy"
	@echo "clean   - remove cdk.out, node_modules, coverage, .venv, .pytest_cache"

# eslint 9 / jest 30 support Node 22 or 24; Node 23 corrupts native postinstalls.
_node_guard:
	@major=$$(node -p "process.versions.node.split('.')[0]"); \
	case "$$major" in \
	  22|24) : ;; \
	  *) echo "cdk: Node $$major is unsupported — use Node 22 or 24."; exit 1 ;; \
	esac

install: _node_guard
	@if test -f package-lock.json; then npm ci; else npm install; fi
	uv sync --locked --group dev

build:
	npm run build

fix:
	npm run format
	npm run lint:fix

api:
	$(MAKE) -C ../api build

check: api
	npx tsc --noEmit
	npm run lint
	npm test
	$(MAKE) test-python

test: api
	npm test -- $(JEST_ARGS)
	$(MAKE) test-python

test-python:
	uv run --locked --group dev ruff check \
		lambdas/connector_ops/ccf.py \
		lambdas/connector_ops/runtime_package_init.py \
		lambdas/connector_ops/register.py \
		test/test_register.py
	uv run --locked --group dev ruff format --check \
		lambdas/connector_ops/ccf.py \
		lambdas/connector_ops/runtime_package_init.py \
		lambdas/connector_ops/register.py \
		test/test_register.py
	uv run --locked --group dev mypy --strict \
		lambdas/connector_ops/ccf.py \
		lambdas/connector_ops/register.py
	uv run --locked --group dev pytest test/test_register.py

deploy: _node_guard api
	npx cdk deploy

clean:
	rm -rf cdk.out node_modules coverage .venv .pytest_cache
