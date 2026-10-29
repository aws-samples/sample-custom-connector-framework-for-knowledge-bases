# Custom Connector Framework — root automation entry point.
# Each verb is fanned across the sub-projects in a fixed order (api first, since
# backend, cdk, and samples consume its generated model). A sub-project is
# skipped if it has no Makefile yet.

SUBPROJECTS := api toolkit backend cdk samples

.PHONY: help install build fix check test deploy clean

help:
	@echo "Custom Connector Framework"
	@echo "  make install   Install dependencies across $(SUBPROJECTS)"
	@echo "  make build     Build/package every artifact"
	@echo "  make fix       Auto-format and lint --fix (mutating)"
	@echo "  make check     Lint + type-check + unit tests (non-mutating)"
	@echo "  make test      Tests with coverage"
	@echo "  make deploy    Deploy / publish"
	@echo "  make clean     Remove build artifacts"
	@echo ""
	@echo "Scope a verb to one project: make -C <api|toolkit|backend|cdk|samples> <verb>"

install build fix check test deploy clean:
	@for project in $(SUBPROJECTS); do \
		if [ -f "$$project/Makefile" ]; then \
			echo "==> $$project: $@"; \
			$(MAKE) --no-print-directory -C $$project $@ || exit $$?; \
		else \
			echo "==> $$project: skipped (no Makefile yet)"; \
		fi; \
	done
