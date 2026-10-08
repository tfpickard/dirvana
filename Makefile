# dirvana build, test and benchmark entry points. See AGENTS.md for conventions.

UV      ?= uv
RUN     := $(UV) run --frozen
ZSH     ?= zsh
ZSH_SRC := dirvana.plugin.zsh init.zsh shell/zsh/dirvana.zsh $(wildcard shell/zsh/functions/*)

.PHONY: all test lint format typecheck zsh-check pytest bench test-audit install zcompile clean

all: test

## test: everything that must be green before a push (linters, type checkers, all tests)
test: lint typecheck zsh-check pytest

lint:
	$(RUN) ruff check src tests
	$(RUN) ruff format --check src tests

format:
	$(RUN) ruff format src tests
	$(RUN) ruff check --fix src tests

typecheck:
	$(RUN) mypy
	$(RUN) pyright

zsh-check:
	@for f in $(ZSH_SRC); do $(ZSH) -n $$f || exit 1; done
	@echo "zsh -n: ok"

pytest:
	$(RUN) pytest -q

## bench: hook latency (in-process p50/p95/p99) and interactive startup cost
bench:
	./bench/run.sh

## test-audit: Linux only; strace the hook and assert every write lands in dirvana's dirs
test-audit:
	$(RUN) pytest -q tests/audit

## zcompile: precompile the plugin (optional; skipped for read-only checkouts)
zcompile:
	@for f in $(ZSH_SRC); do [ -w "$$(dirname $$f)" ] && $(ZSH) -fc "zcompile $$f"; done; true

install: zcompile
	$(UV) tool install --force .

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache dist build
	find . -name '*.zwc' -delete
