# CDK app for the GitHub connector (TypeScript + npm). It builds its one stack only from the
# framework's constructs in ../../../framework/cdk, through the npm workspace at the
# repository root.
# `check` is the non-mutating gate; `fix` is its mutating counterpart.

.PHONY: install build fix check test deploy deploy-destination clean

JEST_ARGS ?=
# The npm workspace root, which owns the one lockfile and node_modules shared with the other CDK apps.
WORKSPACE_ROOT := ../../..
FRAMEWORK_API := $(WORKSPACE_ROOT)/framework/api
# The deployer configuration file `deploy` passes to the app: make deploy CONFIG=config/<file>.json.
CONFIG ?= config/default.json
CDK_CONTEXT = --context config=$(CONFIG)


# eslint 10 / jest 30 support Node 22 or 24; Node 23 corrupts native postinstalls.
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

build:
	npm run build

fix:
	npm run format
	npm run lint:fix

# The framework constructs read the packaged OpenAPI document, which framework/api's build writes.
PACKAGED_OPENAPI := $(FRAMEWORK_API)/packages/ccf-api-models/src/ccf_api_models/openapi.json
$(PACKAGED_OPENAPI):
	@echo "$(PACKAGED_OPENAPI) is missing: run make -C $(FRAMEWORK_API) build first." >&2; exit 1

check: $(PACKAGED_OPENAPI)
	npx tsc --noEmit
	npx prettier --check .
	npm run lint
	npm test

test: $(PACKAGED_OPENAPI)
	npm test -- $(JEST_ARGS)

# The connector stack references an existing destination, so the destination stack deploys first:
# make deploy-destination. Its settings are in DESTINATION_CONFIG, relative to this directory.
DESTINATION_CONFIG ?= config/destination.json

deploy-destination: $(PACKAGED_OPENAPI)
	$(NODE_GUARD)
	npx cdk deploy --all --context destinationConfig=$(DESTINATION_CONFIG)

deploy: $(PACKAGED_OPENAPI)
	$(NODE_GUARD)
	npx cdk deploy GitHubCustomConnector $(CDK_CONTEXT)

clean:
	rm -rf cdk.out coverage
