# TRAIL — control surface.
#
# Between them these targets are the whole demo: bring the stack up, hold a
# conversation, watch the pipeline behind it. Run `make` for the list.
#
# Everything that needs the compose network runs inside it; everything that
# does not (formatting, linting, unit tests) runs on the host with uv, so a
# reviewer can lint and test the repo without Docker or an API key.

COMPOSE ?= docker compose
UV      ?= uv

# Published host ports. Two TRAIL instances on one machine collide on all three,
# so override the ones that clash:
#
#   make up AGENT_PORT=8010 POSTGRES_PORT=55432 UI_PORT=5273
#
# The same variables have to be passed to `down`, `logs`, `chat`, `eval` and
# `test-integration`, since they also build the host-side addresses below.
#
# UI_PORT defaults to 5173 — Vite's own port — so that the containerised UI and
# `make ui-dev` are reached at the same address and muscle memory survives the
# switch. Run both at once and they collide; that is the intended trade, since
# running both at once means you have two copies of the same app.
AGENT_PORT        ?= 8000
POSTGRES_PORT     ?= 5432
UI_PORT           ?= 5173
LANGFUSE_WEB_PORT ?= 3000

# Host-side addresses for anything run outside the compose network. The
# defaults in .env name compose services, which do not resolve from a laptop —
# so the overrides live here, on the command line. Putting them in .env would
# fix the host and break every container.
HOST_ENV := \
	TRAIL_DATABASE_URL=postgresql://trail:trail@localhost:$(POSTGRES_PORT)/trail \
	TRAIL_AGENT_BASE_URL=http://localhost:$(AGENT_PORT) \
	TRAIL_OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:$(LANGFUSE_WEB_PORT)/api/public/otel/v1/traces

.DEFAULT_GOAL := help
.PHONY: help up down logs chat eval intents reconcile ui-dev test matrix test-integration fmt lint clean

help: ## List the targets
	@grep -hE '^[a-z][a-z-]*:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1;36m%-18s\033[0m %s\n", $$1, $$2}'

up: .env ## Build the images and start agent, ui, postgres and the Langfuse stack (ports override: make up AGENT_PORT=8010 POSTGRES_PORT=55432 UI_PORT=5273)
	$(COMPOSE) up --build --detach --wait
	@printf '  waiting for langfuse'; \
	for i in $$(seq 1 60); do \
	  curl -sf http://localhost:$(LANGFUSE_WEB_PORT)/api/public/health >/dev/null && break; \
	  printf '.'; sleep 3; \
	done; printf '\n'
	@printf '\n  agent     http://localhost:%s/docs\n  langfuse  http://localhost:%s\n' \
		'$(AGENT_PORT)' '$(LANGFUSE_WEB_PORT)'
	@printf '            sign in once: %s / %s\n\n  make chat  to talk to it\n\n' \
		"$$(grep -E '^LANGFUSE_INIT_USER_EMAIL=' .env | cut -d= -f2)" \
		"$$(grep -E '^LANGFUSE_INIT_USER_PASSWORD=' .env | cut -d= -f2)"

down: ## Stop the stack, keeping the database volume
	$(COMPOSE) down --remove-orphans

logs: ## Follow logs from every service
	$(COMPOSE) logs --follow --tail=100

chat: .env ## Hold a conversation with the agent from the CLI
	$(COMPOSE) run --rm client trail chat

eval: .env ## Run the mounted example's golden set against the running stack (`make up` first)
	$(COMPOSE) run --rm client trail eval

# The operator's two commands, and they run on the HOST rather than through
# `compose run client`: the client service waits for the agent to be healthy,
# and the whole point of these is that they still work when the agent is the
# thing that is down. They reach Postgres directly — see docs/runbook.md.
intents: ## List the intents waiting on a person (reads Postgres; works with the agent down)
	@$(HOST_ENV) $(UV) run trail intents

reconcile: ## Resolve one UNKNOWN intent by asking the bank: make reconcile INTENT=pix_abc123
	@test -n "$(INTENT)" || { echo 'usage: make reconcile INTENT=pix_abc123' >&2; exit 2; }
	@$(HOST_ENV) $(UV) run trail reconcile $(INTENT)

ui-dev: ## Run the Vite dev server on the host against the running stack (`make up` first)
	cd ui && npm install && npm run dev

test: ## Run the unit tests with coverage (fails under 90%) — offline, no Docker, no database, no API key
	@$(HOST_ENV) TRAIL_LLM_API_KEY=unit-tests-never-call-the-api \
		$(UV) run --extra dev pytest -m unit --cov

matrix: ## Print the invariant matrix (pytest over the control plane, no LLM, no Docker)
	@$(HOST_ENV) TRAIL_LLM_API_KEY=matrix-never-calls-the-api \
		$(UV) run --extra dev pytest -m matrix -p no:randomly -q -s --no-header

test-integration: ## Run the integration tests against the running stack (`make up` first)
	@$(HOST_ENV) $(UV) run --extra dev pytest -m integration

fmt: ## Format and apply safe lint fixes
	$(UV) run --extra dev ruff format .
	$(UV) run --extra dev ruff check --fix .

lint: ## Check formatting and lint without changing anything
	$(UV) run --extra dev ruff format --check .
	$(UV) run --extra dev ruff check .

clean: ## Tear the stack down, drop the database volume, remove caches
	$(COMPOSE) down --volumes --remove-orphans
	rm -rf .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

# `.env` is a real file target, so this fires exactly once — when it is
# missing — and every target that needs credentials depends on it.
.env:
	@echo 'No .env found. Run:  cp .env.example .env  then set TRAIL_LLM_API_KEY' >&2
	@exit 1
