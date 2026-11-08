# Root automation entry point. Two projects, each with its own cdk/ app and Makefile:
# framework/ (the Custom Connector Framework service and libraries) and connectors/github/
# (the GitHub connector). Each verb fans across them in a fixed order: the framework
# first, since the connector builds on its libraries. build, check, and clean also cover the
# documentation site in docs/, which Zensical builds into site/ (preview it with
# `uv run --only-group docs zensical serve`).

PROJECTS := framework connectors/github
DOCS_BUILD := uv run --locked --only-group docs zensical build --strict
# Runs the current target in each project, in order.
define IN_PROJECTS
@for project in $(PROJECTS); do \
	echo "==> $$project: $@"; \
	$(MAKE) --no-print-directory -C $$project $@ || exit $$?; \
done
endef

.PHONY: install build fix check test deploy clean

install fix test:
	$(IN_PROJECTS)

build:
	$(IN_PROJECTS)
	$(DOCS_BUILD)

clean:
	$(IN_PROJECTS)
	rm -rf site .cache

# check also builds the documentation site, failing on a broken link, and scans the working tree for
# secrets; gitleaks (https://github.com/gitleaks/gitleaks) must be on PATH.
check:
	$(IN_PROJECTS)
	$(DOCS_BUILD)
	gitleaks dir .

# Deploys in order: the framework's two stacks, then the GitHub connector's destination stack, then
# the GitHub connector, because each reads the exports or IDs of the one before. Each CONFIG is
# relative to its project's cdk/ directory, for example:
#   make deploy FRAMEWORK_CONFIG=config/default.json DESTINATION_CONFIG=config/destination.json \
#     GITHUB_CONFIG=config/default.json
# The same steps by hand: make -C framework deploy CONFIG=...; make -C connectors/github
# deploy-destination DESTINATION_CONFIG=...; make -C connectors/github deploy CONFIG=...
deploy:
	@test -n "$(FRAMEWORK_CONFIG)" -a -n "$(DESTINATION_CONFIG)" -a -n "$(GITHUB_CONFIG)" || { \
		echo "deploy: set FRAMEWORK_CONFIG, DESTINATION_CONFIG, and GITHUB_CONFIG to your configuration files."; exit 1; }
	$(MAKE) --no-print-directory -C framework deploy CONFIG=$(FRAMEWORK_CONFIG)
	$(MAKE) --no-print-directory -C connectors/github deploy-destination DESTINATION_CONFIG=$(DESTINATION_CONFIG)
	$(MAKE) --no-print-directory -C connectors/github deploy CONFIG=$(GITHUB_CONFIG)
