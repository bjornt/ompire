SHELL := /bin/bash

.DEFAULT_GOAL := help

# Extra args forwarded to the underlying command, e.g.:
#   make test-backend ARGS="-k test_foo"
ARGS ?=

.PHONY: help build build-frontend build-backend run clean \
        test test-backend test-frontend \
        lint lint-backend lint-frontend \
        typecheck typecheck-backend typecheck-frontend \
        update-adr-index

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

## Build

build: build-frontend build-backend ## Build frontend and backend

build-frontend: ## Build the React frontend
	cd frontend && pnpm build

build-backend: ## Install backend dependencies
	cd daemon && uv sync

## Run

run: ## Run the daemon (serves frontend/dist at /)
	cd daemon && uv run ompire-daemon

## Test

test: test-backend test-frontend ## Run all tests

test-backend: ## Run backend tests
	cd daemon && uv run pytest $(ARGS)

test-frontend: ## Run frontend tests
	cd frontend && pnpm test $(ARGS)

## Lint

lint: lint-backend lint-frontend ## Lint everything

lint-backend: ## Lint backend
	cd daemon && uvx ruff check src tests $(ARGS)

lint-frontend: ## Lint frontend
	cd frontend && pnpm lint $(ARGS)

## Typecheck

typecheck: typecheck-backend typecheck-frontend ## Typecheck everything

typecheck-backend: ## Typecheck backend
	cd daemon && uv run --with mypy mypy src $(ARGS)

typecheck-frontend: ## Typecheck frontend
	cd frontend && pnpm exec tsc -b $(ARGS)

## Documentation

update-adr-index: ## Regenerate the ADR index
	python3 scripts/update-adr-index.py

## Clean

clean: ## Remove build artifacts and caches
	rm -rf frontend/dist frontend/node_modules
	rm -rf daemon/.venv
	find daemon -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf daemon/.pytest_cache daemon/.mypy_cache daemon/.ruff_cache
