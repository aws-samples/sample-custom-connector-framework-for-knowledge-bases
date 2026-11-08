# Root automation entry point. Two projects, each with its own cdk/ app and Makefile:
# framework/ (the Custom Connector Framework service and libraries) and connectors/github/
# (the GitHub connector). Each verb fans across them in a fixed order: the framework
# first, since the connector builds on its libraries.

PROJECTS := framework connectors/github

.PHONY: install build fix check test deploy clean

install build fix test clean:
	@for project in $(PROJECTS); do \
		echo "==> $$project: $@"; \
		$(MAKE) --no-print-directory -C $$project $@ || exit $$?; \
	done

# check also scans the working tree for secrets; gitleaks (https://github.com/gitleaks/gitleaks) must be on PATH.
check:
	@for project in $(PROJECTS); do \
		echo "==> $$project: $@"; \
		$(MAKE) --no-print-directory -C $$project $@ || exit $$?; \
	done
	gitleaks dir .

# Deploys in order: the framework's two stacks, then the destination, then the GitHub connector,
# because each reads the exports or IDs of the one before. Each CONFIG is relative to its project's
# cdk/ directory, for example:
#   make deploy FRAMEWORK_CONFIG=config/default.json DESTINATION_CONFIG=config/destination-github.json \
#     GITHUB_CONFIG=config/default.json
# The same steps by hand: make -C framework deploy CONFIG=...; make -C framework/cdk deploy
# DESTINATION_CONFIG=...; make -C connectors/github deploy CONFIG=...
deploy:
	@test -n "$(FRAMEWORK_CONFIG)" -a -n "$(DESTINATION_CONFIG)" -a -n "$(GITHUB_CONFIG)" || { \
		echo "deploy: set FRAMEWORK_CONFIG, DESTINATION_CONFIG, and GITHUB_CONFIG to your configuration files."; exit 1; }
	$(MAKE) --no-print-directory -C framework deploy CONFIG=$(FRAMEWORK_CONFIG)
	$(MAKE) --no-print-directory -C framework/cdk deploy DESTINATION_CONFIG=$(DESTINATION_CONFIG)
	$(MAKE) --no-print-directory -C connectors/github deploy CONFIG=$(GITHUB_CONFIG)
