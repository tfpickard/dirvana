# AGENTS.md: conventions for working on dirvana

dirvana is a zsh hook plus a Python daemon/CLI that records per-directory shell activity into a
shadow tree and turns it into command suggestions. Read `docs/hook-protocol.md` before touching
the hook or the on-disk format.

## Layout

| Path | What |
|---|---|
| `dirvana.plugin.zsh`, `init.zsh` | Plugin entry points (Antidote/plain `source`; Zim) |
| `shell/zsh/dirvana.zsh` | The zsh adapter: hooks, capture, redaction, ignore rules |
| `shell/zsh/functions/` | Autoloaded cold-path zsh functions (recon job, widgets) |
| `src/dirvana/` | Python package: CLI, store, policy, identity, ranking, egress, prompts, enrichment, daemon, hotkey client (stdlib only at runtime; SDKs are extras) |
| `src/dirvana/providers/` | One adapter per provider kind behind `providers/base.py`; only reachable through `egress.py` |
| `tests/vectors/` | Golden vectors shared by the zsh and Python implementations |
| `tests/unit/` | Python unit tests |
| `tests/zsh/` | Tests that drive the zsh functions directly |
| `tests/scenarios/` | End-to-end scenarios: real interactive zsh in a pty, hermetic temp HOME |
| `tests/contract/` | Shared provider contract suite (mocked transports; Copilot lockdown) |
| `bench/` | Hook latency and startup benchmarks |

## Build and test

```sh
uv sync            # dev environment (Python >= 3.11)
make test          # ruff, ruff format --check, mypy --strict, pyright, zsh -n, pytest
make bench         # hook p50/p95/p99 and startup delta
make format        # apply ruff formatting and safe fixes
```

`make test` must be green before every push. Never skip, weaken or delete a test to get
there. Tests are hermetic: they set `HOME`, `ZDOTDIR`, `DIRVANA_ROOT`, `DIRVANA_CONFIG_DIR`,
`DIRVANA_STATE_DIR` to temp dirs and never touch the network.

Test knobs (environment): `DIRVANA_RECON_SYNC=1` runs recon in the foreground,
`DIRVANA_GRACE=0` removes the journal-rewrite grace period, `DIRVANA_NOW=<epoch>` freezes the
clock for ranking, `DIRVANA_MOCK_CAPTURE=<file>` makes `kind = "mock"` providers append every
payload they receive. Tests configure providers as mocks under the real instance names
(`[providers.anthropic] kind = "mock"`) so egress assertions see which instance got what.
Real providers are only exercised by `make smoke` (needs your keys) and by `tests/contract`
against mocked transports.

## Rules that are not negotiable

- **Hot path**: preexec/precmd do zero forks, hold no file descriptors, run no Python and no
  network. chpwd may fork at most one disowned recon job. Budget: < 5 ms p99 per hook, < 10 ms
  added to startup. Run `make bench` after touching `shell/zsh/`.
- **Never write inside observed directories.** Everything goes under the root, config or state
  dirs. Git is only run read-only with `GIT_OPTIONAL_LOCKS=0`.
- **Privacy floor before storage**: leading-space commands, ignored subtrees and redaction are
  enforced by the hook. Redaction rules exist twice (`shell/zsh/dirvana.zsh`,
  `src/dirvana/redact.py`); change both and extend `tests/vectors/redact.json`.
- **Shared semantics live in vectors**: shadow-path mapping, ignore-pattern matching, capture
  heuristics and redaction are each pinned by a file in `tests/vectors/` that both
  implementations must pass.
- **Canonical vs derived**: `<root>/system` is canonical; `<root>/derived` is an LLM cache that
  must always be rebuildable. Policy lives in the config dir, never in the root.
- **Never execute suggestions; never give a provider tools.** The provider interface takes
  text only. The Copilot adapter's session config is asserted against the installed SDK's
  real signature (`tests/contract`); update that test, not the lockdown, when the SDK moves.
- **Egress**: every provider call goes through `EgressGuard`; prompt builders must check
  each item that names another directory (`guard.allows` / `command_allowed`).

## Style

- Python: type hints everywhere, `mypy --strict` and pyright strict clean, ruff clean, no
  runtime dependencies outside the stdlib (provider SDKs are optional extras).
- zsh: entry points (hooks, widgets, load) start with `emulate -L zsh` + `setopt
  extended_glob`; internal helpers inherit those options (a function call costs microseconds
  on the hot path). `typeset -g` for globals, readable over clever, a comment for every
  non-obvious expansion. Never `$(...)` on the hot path or at load: it forks.
- The program name lives in one constant per language (`dirvana._meta.NAME`, zsh
  `_DIRVANA_NAME`); derive names from it.
- Instructions for humans use nvim (never nano), brew on macOS, paru on Linux.
