# SPDX-License-Identifier: Apache-2.0
# Developer entry points. Everything runs against the local checkout with
# src/ on the path — no install step required for test/lint/eval.
#
#   make test   run the full pytest suite (no network, no live servers)
#   make lint   ruff check
#   make eval   run the shipped deterministic eval set in injected (offline) mode
#   make doctor run the full preflight (WARN-passes on a bare machine)
#   make check  lint + test + eval + doctor (the CI gate, locally)
#
# PYTHON lets a caller point at a specific interpreter/venv, e.g.
#   make test PYTHON=.venv/bin/python

PYTHON ?= python
PYTHONPATH := src

export PYTHONPATH

.PHONY: test lint eval doctor check help

help:
	@echo "targets: test | lint | eval | doctor | check"

test:
	$(PYTHON) -m pytest tests/ -q

lint:
	$(PYTHON) -m ruff check .

eval:
	$(PYTHON) -m cheapskate.cli eval

doctor:
	$(PYTHON) -m cheapskate.cli doctor

check: lint test eval doctor
	@echo "all gates passed"
