.PHONY: bootstrap test lint build pair-health release-check

PYTHON ?= .venv/bin/python

bootstrap:
	./run --no-credentials --check

test:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m ruff check src tests scripts

build:
	$(PYTHON) -m build

pair-health:
	$(PYTHON) -m arbx.pairs.health

release-check: lint test build
	$(PYTHON) -m arbx.release_check
