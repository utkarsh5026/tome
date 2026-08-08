.PHONY: test lint run install site binary help

help:
	@echo "test     run the test suite"
	@echo "lint     ruff check (lint only, never format — see CLAUDE.md)"
	@echo "run      serve this repo with tome (ARGS='--port 7979')"
	@echo "install  install the tome CLI from the working tree"
	@echo "site     build + preview the GitHub Pages site on :8000"
	@echo "binary   build a standalone executable for THIS platform into dist/"

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

# Only ever builds for the machine you're on — PyInstaller can't cross-compile,
# which is why the release matrix in .github/workflows/publish.yml exists. Same
# flags as that workflow, minus the --strip it adds on Linux (it's ~2.5MB, and
# stripping is the one flag that isn't safe on every platform). The workflow is
# the one that ships; keep these in step with it.
binary:
	uvx --from pyinstaller pyinstaller --onefile --noupx --console --name tome \
	  --exclude-module tkinter --exclude-module unittest --exclude-module sqlite3 \
	  --exclude-module lzma --exclude-module bz2 tome.py
	@echo "→ dist/tome"
