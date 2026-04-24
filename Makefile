# Siyadah Orchestrator — dev/ops tasks
#
# Requires local Postgres 16 + Redis 7. See CLAUDE.md "Running the test
# suite" section for first-time setup.

SHELL := /bin/bash
PY    := .venv_test/bin/python
PIP   := .venv_test/bin/pip

TEST_DB    ?= postgresql+asyncpg://sy:sy@127.0.0.1:5432/siyadah_test
TEST_REDIS ?= redis://127.0.0.1:6380/0

.PHONY: help venv install test test-quick test-slow lint seed-help audit \
        clean-venv clean-pyc

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*?## "}{printf "  \033[1;36m%-18s\033[0m %s\n",$$1,$$2}'

venv:  ## Create the test virtualenv at .venv_test
	python3 -m venv .venv_test

install: venv  ## Install all runtime + test deps
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt pytest pytest-asyncio ruff

test:  ## Run the full harsh suite against local PG/Redis
	TEST_DATABASE_URL="$(TEST_DB)" TEST_REDIS_URL="$(TEST_REDIS)" \
	SIYADAH_SKIP_PG_SSL=1 \
	$(PY) -m pytest tests/ --ignore=tests/integration_phase_1_4.py -v

test-quick:  ## Run non-slow tests only
	TEST_DATABASE_URL="$(TEST_DB)" TEST_REDIS_URL="$(TEST_REDIS)" \
	SIYADAH_SKIP_PG_SSL=1 \
	$(PY) -m pytest tests/ --ignore=tests/integration_phase_1_4.py \
	    -m "not slow" -v

test-slow:  ## Only the concurrency / long tests
	TEST_DATABASE_URL="$(TEST_DB)" TEST_REDIS_URL="$(TEST_REDIS)" \
	SIYADAH_SKIP_PG_SSL=1 \
	$(PY) -m pytest tests/ -m slow -v

lint:  ## ruff lint (informational)
	.venv_test/bin/ruff check --select E,F,W \
	    --exclude tests/integration_phase_1_4.py .

seed-help:  ## Show seed_tenant_key.py usage
	$(PY) scripts/seed_tenant_key.py --help

audit:  ## Last-24h audit stats against DATABASE_URL
	$(PY) scripts/audit_stats.py

clean-pyc:  ## Remove __pycache__ and *.pyc
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete

clean-venv:  ## Blow away .venv_test
	rm -rf .venv_test
