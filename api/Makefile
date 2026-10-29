.PHONY: help install build contract client model check test fix clean deploy

SMITHY_VERSION := 1.74.1
SMITHY_PLATFORM := $(shell uname -s | tr '[:upper:]' '[:lower:]')-$(shell uname -m | sed 's/arm64/aarch64/')
# SHA-256 of each smithy-cli-<platform>.zip attached to the Smithy CLI GitHub release.
SMITHY_SHA256_darwin-aarch64 := ebb5e931936aa503055b4620c4981319f2b060656482553ada0151235ff0f1ac
SMITHY_SHA256_darwin-x86_64 := 06b5fa6660fd3ab0281511af1d04f3128e10fa6220c552bc2b94ff18d660886d
SMITHY_SHA256_linux-aarch64 := c563b5387c66fe04b8c459178dfcc08316798bc107c03fd92474dd8a0c028850
SMITHY_SHA256_linux-x86_64 := 6b50c0b9f2443400bb7115b9badb54b3609816ec5fb4f625954eadd2f6b2f52a
SMITHY_RELEASE := smithy-cli-$(SMITHY_VERSION)-$(SMITHY_PLATFORM)
SMITHY := .smithy-cli/current/bin/smithy
GENERATED := generated
CODEGEN := codegen
AST := $(GENERATED)/model.json
OPENAPI := $(GENERATED)/openapi.json
MODELS_PY := packages/ccf-api-models/src/ccf_api_models/models.py
CLIENT_PY := ../toolkit/src/custom_connector_framework/_generated_client.py

help:
	@echo "install - uv sync and install the pinned, checksum-verified Smithy CLI"
	@echo "build   - emit the AST, OpenAPI, Pydantic models, and typed client operations"
	@echo "client  - write the private typed client operations from generated OpenAPI"
	@echo "check   - regenerate contracts, then verify client freshness, lint, types, and HTTP contract"
	@echo "test    - assert strict generated Pydantic contract behavior"
	@echo "fix     - ruff --fix and format codegen, then rebuild"
	@echo "clean   - remove generated artifacts and build output"

install:
	uv sync --extra dev
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

$(GENERATED):
	mkdir -p $(GENERATED)

# Emit the Smithy AST used to derive generic operation contract assertions.
model: $(GENERATED)
	$(SMITHY) ast --aut > $(AST)

# OpenAPI and strict Pydantic models are generated from the Smithy source of truth.
contract: model
	$(SMITHY) build
	cp build/smithy/source/openapi/*.openapi.json $(OPENAPI)
	uv run python $(CODEGEN)/enrich_openapi.py --model $(AST) --openapi $(OPENAPI)
	uv run datamodel-codegen \
		--input $(OPENAPI) --input-file-type openapi \
		--output $(MODELS_PY) --output-model-type pydantic_v2.BaseModel \
		--use-standard-collections --use-union-operator \
		--target-python-version 3.14 --disable-timestamp --use-schema-description \
		--field-constraints --extra-fields forbid \
		--base-class ccf_api_models.base.CcfModel
	@echo "built: $(OPENAPI), $(MODELS_PY)"

client: contract
	uv run python $(CODEGEN)/generate_client.py --openapi $(OPENAPI) --output $(CLIENT_PY)

build: client

check: contract
	$(SMITHY) validate
	uv run ruff check $(CODEGEN)/
	MYPYPATH=packages/ccf-api-models/src uv run mypy $(CODEGEN)/
	uv run python $(CODEGEN)/assert_contract.py --model $(AST) --openapi $(OPENAPI)
	uv run python $(CODEGEN)/generate_client.py --openapi $(OPENAPI) --output $(CLIENT_PY) --check

test: build
	PYTHONPATH=packages/ccf-api-models/src uv run python $(CODEGEN)/assert_generated_models.py

fix:
	uv run ruff check --fix $(CODEGEN)/
	uv run ruff format $(CODEGEN)/
	$(MAKE) build

clean:
	rm -rf $(GENERATED) build .ruff_cache .mypy_cache

# The deployable artifacts are the OpenAPI document (API Gateway import) and the
# ccf-api-models package consumed by the toolkit and samples.
deploy:
	@echo "deploy: api has no deployable; its OpenAPI document and Pydantic package feed the toolkit and samples"
