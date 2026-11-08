# CDK (TypeScript + npm). `check` is the non-mutating gate; `fix` is its
# mutating counterpart. Verbs mirror backend/ and toolkit/ so the root Makefile
# can fan one verb across every sub-project.

.PHONY: install build fix check test deploy clean

JEST_ARGS ?=
# The npm workspace root, which owns the one lockfile and node_modules shared with the other CDK apps.
WORKSPACE_ROOT := ../..
# The deployer configuration file `deploy` passes to the app: make deploy CONFIG=config/<file>.json.
CONFIG ?= config/default.json
CDK_CONTEXT = --context config=$(CONFIG)

# uv can pick an x86_64 Python on Apple Silicon, which forces native wheels to build from source.
ifeq ($(shell uname -sm),Darwin arm64)
UV_VENV_PYTHON := --python cpython-3.14-macos-aarch64-none
endif


# eslint 9 / jest 30 support Node 22 or 24; Node 23 corrupts native postinstalls.
define NODE_GUARD
@major=$$(node -p "process.versions.node.split('.')[0]"); \
case "$$major" in \
  22|24) : ;; \
  *) echo "cdk: Node $$major is unsupported. Use Node 22 or 24."; exit 1 ;; \
esac
endef

install:
	$(NODE_GUARD)
	cd $(WORKSPACE_ROOT) && npm ci
	uv venv --allow-existing --quiet $(UV_VENV_PYTHON)
	uv sync --locked --group dev

build:
	npm run build

fix:
	npm run format
	npm run lint:fix

# The app reads the packaged OpenAPI document, which framework/api's build writes.
PACKAGED_OPENAPI := ../api/packages/ccf-api-models/src/ccf_api_models/openapi.json
$(PACKAGED_OPENAPI):
	@echo "$(PACKAGED_OPENAPI) is missing: run make -C ../api build first." >&2; exit 1

check: $(PACKAGED_OPENAPI)
	npx tsc --noEmit
	npm run lint
	npm test
	npx tsx test/validate-state-machines.ts
	$(TEST_PYTHON)

test: $(PACKAGED_OPENAPI)
	npm test -- $(JEST_ARGS)
	$(TEST_PYTHON)

# The custom resource handlers, one module each in lambdas/connector_ops, and their tests.
PYTHON_SOURCES := lambdas/connector_ops
PYTHON_TESTS := test

define TEST_PYTHON
uv run --locked --group dev ruff check $(PYTHON_SOURCES) $(PYTHON_TESTS)
uv run --locked --group dev ruff format --check $(PYTHON_SOURCES) $(PYTHON_TESTS)
uv run --locked --group dev mypy $(PYTHON_SOURCES) $(PYTHON_TESTS)
uv run --locked --group dev pytest $(PYTHON_TESTS)
endef

# The Ops stack reads the service stack's exports, so the service stack deploys first. A tenant's
# destination stack deploys from its own project, for example make -C connectors/github deploy-destination.
deploy: $(PACKAGED_OPENAPI)
	$(NODE_GUARD)
	npx cdk deploy CustomConnectorFramework $(CDK_CONTEXT)
	npx cdk deploy CustomConnectorFrameworkOps $(CDK_CONTEXT)

clean:
	rm -rf cdk.out node_modules coverage .venv .pytest_cache
