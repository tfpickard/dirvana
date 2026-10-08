# dirvana

A per-directory context graph for the shell.

Per-directory history only knows what you typed *in* a directory. What you actually do is
relational: from `acme-build-config-2026q3-variant-a` you keep diffing against, copying from and
listing `acme-build-config-2026q3-variant-b`. dirvana records those edges as they happen,
keeps everything in a human-readable shadow tree that never touches your real directories,
precomputes LLM context per directory in the background, and on a hotkey hands you commands
that already contain the right path.

> **Status: milestone 2 of 6.** Capture, daemon, enrichment, the `^Xj` picker and `^Xb`
> briefing, and three providers (Anthropic, OpenAI, GitHub Copilot) behind per-subtree egress
> pins. Still to come: retention/compaction (M3), export/import/layers (M4), encrypted git sync
> (M5), service units and Docker (M6).

## Install

Requires zsh ≥ 5.8 and Python ≥ 3.11 ([uv](https://docs.astral.sh/uv/) recommended).

```sh
# CLI (provides `dirvana`)
uv tool install git+https://github.com/tfpickard/dirvana

# zsh plugin: pick one
antidote bundle tfpickard/dirvana       # Antidote (add to .zsh_plugins.txt)
zmodule tfpickard/dirvana               # Zim (in .zimrc), then: zimfw install
source /path/to/dirvana/dirvana.plugin.zsh   # plain, in ~/.zshrc
```

`dirvana init zsh` prints a `source` line for the copy of the plugin bundled with the CLI. The
plugin itself never starts Python; it is safe under zsh-defer. On macOS: `brew install zsh uv`;
on Arch: `paru -S zsh uv`.

## What it records

For every command, in the node of the directory it ran in:

```sh
$ cd ~/src/acme-build-config-2026q3-variant-a
$ diff ../acme-build-config-2026q3-variant-b/Makefile Makefile
$ dirvana edges
outbound (1)
 ? ../acme-build-config-2026q3-variant-b   diff×1  (last 2026-10-07, score 0.75)
inbound: none
? = tentative: too little evidence yet; never sent to a provider
$ dirvana edges ../acme-build-config-2026q3-variant-b
outbound: none
inbound (1)
 ? ../acme-build-config-2026q3-variant-a   diff×1  (last 2026-10-07, score 0.75)
```

Edges carry verbs (`list read search copy-from copy-to move-from move-to diff edit cd run
ref`), counts, sessions and a per-day histogram. Ranking applies a configurable recency decay
(half-life 30 days) and an evidence floor, so one stray command never outranks a habit.

On entering a directory the hook also records *recon* in the background: git toplevel,
branch, remotes (credentials stripped), root commit, and bounded, redacted excerpts of
README, AGENTS.md, CLAUDE.md and build manifests. Git runs read-only; dirvana never writes
inside the directories it observes, including ones you cannot write (`/etc`).

## Hotkeys

| Key | Widget | What it does |
|---|---|---|
| `^Xj` | `dirvana-suggest` | Candidate commands for this directory in a picker (fzf if installed, else a numbered list). The pick is placed on the command line; **nothing is ever executed.** |
| `^Xb` | `dirvana-brief` | What this directory is, what changed since you were last here, what you are likely about to do. Printed above the prompt. |

Keys are bound only if still unbound, so your own bindings and other plugins win. Change them
with `zstyle ':dirvana:widget:dirvana-suggest' key '^Xs'` (or `none`); force a picker with
`zstyle ':dirvana:widget' picker builtin|fzf`; set fzf's height with
`zstyle ':dirvana:widget' fzf-height 60%` (or `full`).

Each press makes one provider call with a hard deadline (`hotkey.timeout`, default 2.5 s).
If the provider is slow, down, over budget or not allowed for this directory, you get local
suggestions ranked from your own history and edges instead, within the same deadline. LLM
suggestions are checked before you see them: paths must exist or be one of your known related
directories, and near-misses of those 40-character sibling names are repaired.

## Daemon and providers

```sh
dirvana daemon            # long-running: ingest, enrich dirty directories, serve hotkeys
dirvana daemon --oneshot  # one pass, for a timer (systemd/launchd units arrive in M6)
dirvana enrich [DIR]      # enrich now (asks the daemon if it is running)
dirvana doctor --providers
make smoke                # one tiny real call per configured provider
```

Enrichment only runs for directories whose canonical data changed (a fingerprint of the exact
prompt input); a second run with nothing new makes zero calls. Derived context lives in
`<root>/derived` and is a cache: delete it and the daemon rebuilds it.

Configure providers in `~/.config/dirvana/config.toml` (defaults:
[src/dirvana/data/default-config.toml](src/dirvana/data/default-config.toml)). Install the SDK
extras you need: `uv tool install 'dirvana[anthropic,openai,copilot]'` (or `[all]`).

| Instance | Setup |
|---|---|
| `anthropic` | `export ANTHROPIC_API_KEY=…` or `api_key_cmd = "pass show anthropic"`. Default model `claude-sonnet-5-5`. |
| `openai` | Set `model = "…"` (no default) and `OPENAI_API_KEY`. For Ollama/llama.cpp/LM Studio add your own instance: `[providers.ollama]` with `kind = "openai"`, `base_url = "http://127.0.0.1:11434/v1"`, `model = "…"`. |
| `copilot` | Official GitHub Copilot SDK; needs a Copilot subscription. `auth = "env"` reads `COPILOT_GITHUB_TOKEN` (fine-grained PAT with Copilot access) or `GH_TOKEN`; `auth = "gh"` uses `gh auth token`; `auth = "logged-in"` uses `copilot login`. Runs with **no tools, no MCP servers, no file access**, one fresh session per request; hotkeys need the long-running daemon (the runtime takes seconds to start). |

Budgets are per instance in native units (`max_tokens_per_run/day`, Copilot
`max_requests_per_run/day`); every call is logged to `<root>/var/usage.jsonl`.

### Egress: which provider may see what

`llm=` rules in your policy decide, per subtree, which provider *instances* may receive its
data; `llm=none` keeps it on the machine. A request about a pinned directory goes only to its
allowed instances; if they fail you get local suggestions, never another provider. Context
about *other* directories (edges, session history, derived text) is included only where that
directory allows the instance too, and every outgoing path and secret is checked once more
(by the client and again by the daemon).

```
# ~/.config/dirvana/policy
~/work/acme    llm=copilot          # employer code: Copilot only
~/journal      llm=none             # never leaves this machine
```

`dirvana policy explain DIR` shows the effective rule and the providers that would be used.

## The shadow tree

```
~/.local/share/dirvana/system/home/tom/src/acme-build-config-2026q3-variant-a/
├── %obs.jsonl        observations (one JSON line per command)
├── %recon.json       recon facts and excerpts
├── %edges.out.json   where you reach from here
├── %edges.in.json    where you arrive from
├── %node.json        identity, labels, notes
└── src/…             real subdirectories continue the mirror
```

Node files start with `%`; a real directory whose name starts with `%` is stored with the `%`
doubled, so the two never collide. `tree`, `cat` and `nvim` work on it as-is (`nvim "$(dirvana
path)/%obs.jsonl"`). `rm -rf ~/.local/share/dirvana` is a complete reset, even mid-session.

## CLI

```
dirvana status                     where data lives, how much there is
dirvana show [DIR] [--json]        recon, activity, top commands, edges, policy
dirvana edges [DIR] [--in|--out]   ranked edges from DIR's own vantage point
dirvana path [DIR]                 the node directory in the shadow tree
dirvana note DIR TEXT              attach a note;  dirvana label DIR k=v  set labels
dirvana forget DIR [-r]            delete a node (and subtree) and every reference to it
dirvana policy explain [DIR]       effective policy and which file:line set each field
dirvana policy edit [DIR]          open the global or per-subtree policy file in $EDITOR
dirvana enrich [DIR...]            build derived context now (--force, --dry-run)
dirvana daemon [--oneshot]         the background daemon
dirvana doctor [--providers]       installation, daemon and provider health
dirvana smoke [--provider NAME]    one tiny real call per provider
dirvana pause|resume|incognito     this shell only (--global: every shell)
```

## Privacy

Enforced by the hook *before* anything is written:

* Commands typed with a leading space are not recorded, and neither is a `cd` they perform.
* Ignored subtrees (built in: `~/.ssh`, `~/.gnupg`, password stores, `~/.aws`, `~/.kube`,
  `~/.docker`, `~/.config/gcloud`; plus your `ignore` rules) are never recorded; arguments
  pointing into them become `<ignored>`.
* Secrets are redacted: Authorization/Bearer values, `*_KEY= *_SECRET= *_TOKEN= *PASSWORD*=`
  assignments and `--flag` forms, credentials in URLs, and known token shapes (GitHub,
  Anthropic, OpenAI, Slack, AWS, Google, GitLab, JWTs, private keys).
* `dirvana pause` / `incognito` stop recording in the current shell.

See [docs/policy.md](docs/policy.md) for per-subtree rules and
[docs/hook-protocol.md](docs/hook-protocol.md) for the exact on-disk contract.

## Performance

Budgets: each hook adds < 5 ms at p99, and the plugin adds < 10 ms to interactive startup.
The hot path never forks, holds no file descriptors and starts no interpreter.

Measured with `make bench` on a 4-vCPU Firecracker VM (Intel Xeon @ 2.8 GHz, Linux, zsh 5.9).
This VM is roughly 3–4× slower than a current laptop for interpreter work (an empty zsh
function call costs ~13 µs), so treat these as upper bounds:

| Hook (µs) | p50 | p95 | p99 |
|---|---|---|---|
| preexec (realistic corpus, n=1000) | 535 | 953 | 1259 |
| precmd (writes the record) | 168 | 281 | 390 |
| chpwd (known directory) | 425 | 675 | 837 |
| chpwd, first visit (forks background recon) | 1562 | 3138 | 3692 |
| pathological 4 KiB / 40-path line, preexec+precmd | 1967 | 2870 | 3688 |

| Startup (`zsh -i -c exit`, hyperfine, 300 runs) | median | added |
|---|---|---|
| baseline (prompt only) | 4.41 ms | |
| + dirvana, plain `source` | 10.84 ms | 6.44 ms |
| + dirvana, zcompiled (`make zcompile`, Antidote) | 8.63 ms | 4.23 ms |

(M2 added the two widgets; registering them costs ~0.4 ms. Hotkeys themselves start Python,
but only when pressed.)

**No C helper.** Profiling showed the costs are zsh function-call and pattern overhead, not
anything a helper would remove: a fork+exec of even a trivial binary costs ~1.8 ms on this VM,
more than a whole typical preexec. If you run `make bench` on macOS, please send the numbers.

## Development

See [AGENTS.md](AGENTS.md). In short: `uv sync`, then `make test` (ruff, mypy --strict,
pyright strict, `zsh -n`, pytest: unit tests, shared zsh/Python golden vectors, and end-to-end
scenarios in a real interactive zsh with a hermetic `$HOME`). `make test-audit` adds a Linux
strace pass proving every write lands in dirvana's own directories.

## License

MPL-2.0.
