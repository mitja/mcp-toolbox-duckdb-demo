# Task runner for the MCP Toolbox DuckDB/Quack demo.
# Every target is a thin wrapper over docker compose / the codegen
# script — see README.md for the full walkthroughs.

COMPOSE := docker compose

.PHONY: help build up down clean logs bi dataeng obs all \
        trace load-test agent inspect codegen codegen-check smoke

help: ## List targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

build: ## Build all images (core profiles)
	$(COMPOSE) build

up: ## Start the core stack (quack-servers, toolbox, otel, jaeger)
	$(COMPOSE) up -d --build

down: ## Stop everything (all profiles), keep volumes
	$(COMPOSE) --profile "*" down

clean: ## Stop everything and remove volumes
	$(COMPOSE) --profile "*" down -v

logs: ## Tail logs of the running services
	$(COMPOSE) logs -f

bi: ## Start the BI track (cube + superset) on top of the core stack
	$(COMPOSE) --profile bi up -d --build

dataeng: ## Start the data-engineering track (dagster + quack-server-3)
	$(COMPOSE) --profile dataeng up -d --build

obs: ## Start prometheus + grafana over the collector's metrics
	$(COMPOSE) --profile obs up -d

all: ## Start every profile
	$(COMPOSE) --profile bi --profile dataeng --profile obs up -d --build

trace: ## Emit one test trace through toolbox -> collector -> jaeger
	$(COMPOSE) --profile trace run --rm trace-client

load-test: ## Concurrent multi-source smoke test (exits non-zero on failure)
	$(COMPOSE) --profile load run --rm trace-load

agent: ## Run the LangGraph agent demo (needs ANTHROPIC_API_KEY in .env)
	$(COMPOSE) --profile agent run --rm langgraph

inspect: ## Start the MCP Inspector UI on :6274
	$(COMPOSE) --profile inspect up -d inspector

codegen: ## Regenerate the cube_* tools in tools.yaml from cube/model
	uv run --no-project --with pyyaml python3 cube/gen_toolbox_from_cube.py

codegen-check: codegen ## Fail if tools.yaml is out of sync with cube/model
	git diff --exit-code tools.yaml

smoke: ## CI-style end-to-end check: build, start core, run load test, tear down
	$(COMPOSE) up -d --build --wait
	curl -sf http://localhost:13133 > /dev/null && echo "otel-collector healthy"
	$(COMPOSE) --profile load run --rm trace-load
	$(COMPOSE) --profile "*" down
