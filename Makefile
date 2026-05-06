PYTHON ?= python

.PHONY: install test lint crawl-nc

install:
	$(PYTHON) -m pip install -e .[dev]

test:
	pytest

lint:
	ruff check .

crawl-nc:
	duke-rates crawl --state NC
