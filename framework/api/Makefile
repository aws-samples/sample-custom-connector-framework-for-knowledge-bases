.PHONY: install build check test fix clean deploy

# uv can pick an x86_64 Python on Apple Silicon, which forces native wheels to build from source.
ifeq ($(shell uname -sm),Darwin arm64)
UV_VENV_PYTHON := --python cpython-3.14-macos-aarch64-none
endif

SMITHY_VERSION := 1.74.1
SMITHY_PLATFORM := $(shell uname -s | tr '[:upper:]' '[:lower:]')-$(shell uname -m | sed 's/arm64/aarch64/')
# SHA-256 of each smithy-cli-<platform>.zip attached to the Smithy CLI GitHub release.
SMITHY_SHA256_darwin-aarch64 := ebb5e931936aa503055b4620c4981319f2b060656482553ada0151235ff0f1ac
SMITHY_SHA256_darwin-x86_64 := 06b5fa6660fd3ab0281511af1d04f3128e10fa6220c552bc2b94ff18d660886d
SMITHY_SHA256_linux-aarch64 := c563b5387c66fe04b8c459178dfcc08316798bc107c03fd92474dd8a0c028850
SMITHY_SHA256_linux-x86_64 := 6b50c0b9f2443400bb7115b9badb54b3609816ec5fb4f625954eadd2f6b2f52a
SMITHY_RELEASE := smithy-cli-$(SMITHY_VERSION)-$(SMITHY_PLATFORM)
SMITHY := .smithy-cli/current/bin/smithy
CODEGEN := codegen
PACKAGE_SRC := packages/ccf-api-models/src
PACKAGE := $(PACKAGE_SRC)/ccf_api_models
# Every file build writes, relative to OUT. All are tracked; check rebuilds them into build/check and diffs.
GENERATED_FILES := generated/model.json generated/openapi.json $(PACKAGE)/models.py $(PACKAGE)/operations.py \
	$(PACKAGE)/metric_names.py $(PACKAGE)/job_container.py $(PACKAGE)/openapi.json
OUT ?= .
CHECK_DIR := build/check
CODEGEN_RUN := PYTHONPATH=$(PACKAGE_SRC) uv run --locked python


install:
	uv venv --allow-existing --quiet $(UV_VENV_PYTHON)
	uv sync --locked
	@if [ "$$($(SMITHY) --version 2>/dev/null)" != "$(SMITHY_VERSION)" ]; then \
		test -n "$(SMITHY_SHA256_$(SMITHY_PLATFORM))" || { echo "No pinned Smithy CLI for $(SMITHY_PLATFORM)"; exit 1; }; \
		rm -rf .smithy-cli/download && mkdir -p .smithy-cli/download && \
		curl -fsSL -o .smithy-cli/download/cli.zip https://github.com/smithy-lang/smithy/releases/download/$(SMITHY_VERSION)/smithy-cli-$(SMITHY_PLATFORM).zip && \
		echo "$(SMITHY_SHA256_$(SMITHY_PLATFORM))  .smithy-cli/download/cli.zip" | shasum -a 256 -c - && \
		unzip -q .smithy-cli/download/cli.zip -d .smithy-cli/download && \
		rm -rf .smithy-cli/$(SMITHY_RELEASE) && \
		mv .smithy-cli/download/smithy-cli-$(SMITHY_PLATFORM) .smithy-cli/$(SMITHY_RELEASE) && \
		ln -sfn $(SMITHY_RELEASE) .smithy-cli/current && rm -rf .smithy-cli/download; \
	fi
	@$(SMITHY) --version

# The Smithy AST and OpenAPI document, the enriched document, the Pydantic models, and the operation and
# metric enums, each generated from the one before. OUT=build/check writes them where check diffs them.
build:
	mkdir -p $(OUT)/generated $(OUT)/$(PACKAGE)
	$(SMITHY) ast --aut > $(OUT)/generated/model.json
	$(SMITHY) build
	cp build/smithy/source/openapi/*.openapi.json $(OUT)/generated/openapi.json
	$(CODEGEN_RUN) $(CODEGEN)/enrich_openapi.py --model $(OUT)/generated/model.json --openapi $(OUT)/generated/openapi.json
	uv run --locked datamodel-codegen \
		--input $(OUT)/generated/openapi.json --input-file-type openapi \
		--output $(OUT)/$(PACKAGE)/models.py --output-model-type pydantic_v2.BaseModel \
		--use-standard-collections --use-union-operator --use-type-alias \
		--target-python-version 3.14 --disable-timestamp --use-schema-description \
		--field-constraints --extra-fields forbid --formatters ruff-format ruff-check --strict-nullable \
		--base-class ccf_api_models.base.CcfModel
	$(CODEGEN_RUN) $(CODEGEN)/write_enums.py --openapi $(OUT)/generated/openapi.json \
		--operations $(OUT)/$(PACKAGE)/operations.py --metric-names $(OUT)/$(PACKAGE)/metric_names.py \
		--job-container $(OUT)/$(PACKAGE)/job_container.py
	uv run --locked ruff format --quiet $(OUT)/$(PACKAGE)/operations.py $(OUT)/$(PACKAGE)/metric_names.py \
		$(OUT)/$(PACKAGE)/job_container.py
	cp $(OUT)/generated/openapi.json $(OUT)/$(PACKAGE)/openapi.json

# A stale generated file fails check instead of being rewritten.
check:
	$(SMITHY) validate
	rm -rf $(CHECK_DIR)
	$(MAKE) build OUT=$(CHECK_DIR)
	for file in $(GENERATED_FILES); do diff -u $$file $(CHECK_DIR)/$$file || exit 1; done
	uv run --locked ruff check $(CODEGEN) tests $(PACKAGE)
	uv run --locked ruff format --check $(CODEGEN) tests $(PACKAGE)
	uv run --locked mypy $(CODEGEN) tests $(PACKAGE)
	uv run --locked pytest

test:
	uv run --locked pytest

fix:
	uv run --locked ruff check --fix $(CODEGEN) tests $(PACKAGE)
	uv run --locked ruff format $(CODEGEN) tests $(PACKAGE)

clean:
	rm -rf build .ruff_cache .mypy_cache

# The deployable artifacts are the OpenAPI document (API Gateway import) and the
# ccf-api-models package consumed by the toolkit and the connectors.
deploy:
	@echo "deploy: api has no deployable; its OpenAPI document and Pydantic package feed the toolkit and the connectors"
