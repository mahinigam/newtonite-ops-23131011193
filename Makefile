# Newtonite - Operations Under Pressure
#
# Thin wrapper so a reviewer does not have to remember individual commands.
# Every target wraps a documented command; run `make help` to list them.

BACKEND := backend
FRONTEND := frontend

.PHONY: help setup setup-api setup-web db-up db-down migrate seed \
	dev-api dev-web worker worker-once test test-api test-web lint lint-api lint-web \
	typecheck typecheck-api typecheck-web build-web stack-up stack-down check

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: setup-api setup-web ## Install backend and frontend dependencies

setup-api: ## Install backend dependencies from the lockfile
	cd $(BACKEND) && uv sync --group dev

setup-web: ## Install frontend dependencies from the lockfile
	cd $(FRONTEND) && npm ci

db-up: ## Start the docker-compose PostgreSQL (host port 5433)
	docker compose up -d db

db-down: ## Stop the docker-compose PostgreSQL
	docker compose down

migrate: ## Apply database migrations to head
	cd $(BACKEND) && uv run alembic upgrade head

seed: ## Load deterministic demo data (idempotent; ITEMS=200 for a small set)
	cd $(BACKEND) && uv run python -m scripts.seed --items $(or $(ITEMS),3000)

dev-api: ## Run the API with autoreload on http://localhost:8000
	cd $(BACKEND) && uv run uvicorn app.main:app --reload --port 8000

dev-web: ## Run the frontend dev server on http://localhost:5173
	cd $(FRONTEND) && npm run dev

worker: ## Deliver queued notifications, polling until stopped
	cd $(BACKEND) && uv run python -m app.worker

worker-once: ## Deliver the notifications that are due now, then exit
	cd $(BACKEND) && uv run python -m app.worker --once

stack-up: ## Build and run the whole stack (db + api + worker + web) in Docker
	docker compose up --build

stack-down: ## Stop the whole Docker stack
	docker compose down

test: test-api test-web ## Run every test suite

test-api: ## Run the backend test suite against PostgreSQL
	cd $(BACKEND) && uv run pytest -q

test-web: ## Run the frontend test suite
	cd $(FRONTEND) && npm run test

lint: lint-api lint-web ## Lint both projects

lint-api: ## Format check and lint the backend
	cd $(BACKEND) && uv run ruff format --check . && uv run ruff check .

lint-web: ## Lint the frontend
	cd $(FRONTEND) && npm run lint

typecheck: typecheck-api typecheck-web ## Static type check both projects

typecheck-api: ## Static type check the backend
	cd $(BACKEND) && uv run mypy .

typecheck-web: ## Static type check the frontend
	cd $(FRONTEND) && npm run typecheck

build-web: ## Build the frontend for production (into frontend/dist)
	cd $(FRONTEND) && npm run build

check: lint typecheck test build-web ## Run every quality gate, including the production build
