.PHONY: bootstrap test lint typecheck coverage build pair-health release-check

PYTHON ?= .venv/bin/python

bootstrap:
	./run --no-credentials --check

test:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m ruff check src tests scripts

typecheck:
	$(PYTHON) -m mypy

coverage:
	$(PYTHON) -m pytest -q --cov=arbx --cov-report=term-missing --cov-report=xml

build:
	$(PYTHON) -m build

pair-health:
	$(PYTHON) -m arbx.pairs.health

release-check: lint typecheck test build
	$(PYTHON) -m arbx.release_check
