# Custom Connector Framework (CCF): the service and the libraries other projects build on.
# Each verb fans across the subprojects in a fixed order: api first, since toolkit,
# backend, and cdk consume its generated models.

SUBPROJECTS := api toolkit backend cdk

.PHONY: install build fix check test deploy clean


install build fix check test clean:
	@for project in $(SUBPROJECTS); do \
		echo "==> framework/$$project: $@"; \
		$(MAKE) --no-print-directory -C $$project $@ || exit $$?; \
	done

# Only the CDK app deploys; it bundles backend/ into the service's Lambda functions.
# CONFIG is relative to cdk/, for example CONFIG=config/example-disposable.json.
deploy:
	$(MAKE) --no-print-directory -C cdk deploy $(if $(CONFIG),CONFIG=$(CONFIG))
