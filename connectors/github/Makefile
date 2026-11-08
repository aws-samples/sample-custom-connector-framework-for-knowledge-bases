# GitHub connector: a custom connector that reads public AWS GitHub repositories into a
# destination. backend/ is the connector implementation; cdk/ deploys it. It builds on
# framework/, so run `make install` at the repository root first.

SUBPROJECTS := backend cdk

.PHONY: install build fix check test deploy deploy-destination clean


install build fix check test clean:
	@for project in $(SUBPROJECTS); do \
		echo "==> connectors/github/$$project: $@"; \
		$(MAKE) --no-print-directory -C $$project $@ || exit $$?; \
	done

# The destination stack deploys first: it creates the connector's destination through the framework
# API. DESTINATION_CONFIG is relative to cdk/ and defaults to config/destination.json.
deploy-destination:
	$(MAKE) --no-print-directory -C cdk deploy-destination $(if $(DESTINATION_CONFIG),DESTINATION_CONFIG=$(DESTINATION_CONFIG))

# Only the CDK app deploys; it builds the connector image from backend/.
# CONFIG is relative to cdk/, for example CONFIG=config/example-small.json.
deploy:
	$(MAKE) --no-print-directory -C cdk deploy $(if $(CONFIG),CONFIG=$(CONFIG))
