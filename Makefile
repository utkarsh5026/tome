.PHONY: test lint run install site help

help:
	@echo "test     run the test suite"
	@echo "lint     ruff check (lint only, never format — see CLAUDE.md)"
	@echo "run      serve this repo with tome (ARGS='--port 7979')"
	@echo "install  install the tome CLI from the working tree"
	@echo "site     build + preview the GitHub Pages site on :8000"

test:
	python3 -m unittest

lint:
	uvx ruff check .

run:
	python3 tome.py $(ARGS)

install:
	uv tool install . --force

site:
	python3 site/build.py --serve
