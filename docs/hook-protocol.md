# Hook protocol (v1)

The contract between a shell adapter (zsh today; bash and fish later) and the rest of dirvana.
An adapter that follows this document and passes `tests/vectors/` is a correct adapter.

## Directories

| Name | Default | Override |
|---|---|---|
| root (data) | `${XDG_DATA_HOME:-~/.local/share}/dirvana` | `DIRVANA_ROOT` |
| config (user intent) | `${XDG_CONFIG_HOME:-~/.config}/dirvana` | `DIRVANA_CONFIG_DIR` |
| state (per machine) | `${XDG_STATE_HOME:-~/.local/state}/dirvana` | `DIRVANA_STATE_DIR` |

Relative XDG values are invalid and ignored. The adapter creates the root with mode `0700`
and node directories with `0700`; record files are `0600`.

## Shadow mapping

A real absolute *physical* path (symlinks resolved, as `realpath(3)`) maps to a directory under
`<root>/system`:

* `/` maps to `<root>/system`; `/a/b` maps to `<root>/system/a/b`.
* A path component that starts with `%` is stored with the `%` doubled (`%x` → `%%x`).
* Names in a shadow directory that start with exactly one `%` are node files. They never
  collide with mirrored children.

Vectors: `tests/vectors/shadow.json`.

## Node files written by the adapter

| File | Written how |
|---|---|
| `%obs.jsonl` | Append only. Open, one `write(2)` of the full record plus `\n`, close. Never hold the descriptor across commands: compaction renames the file. |
| `%recon.json` | Write to `%recon.json.tmp.<pid>`, then `rename(2)` over the target. |

Nothing else is written, and nothing is ever written outside the root and state directories.

## Observation record

One JSON object per line, UTF-8 where the input was UTF-8 (other bytes pass through and are
decoded by readers with `surrogateescape`).

```json
{"v":1,"id":"<mid>:<pid>:<start>:<seq>","k":"cmd","t":1791416384.530,"mid":"<mid>",
 "host":"<hostname>","sid":"<mid>:<pid>:<start>","cwd":"/abs/physical/cwd",
 "cmd":"<redacted command as typed>","st":0,"dur":0.008,
 "paths":[{"verb":"diff","arg":"../B/f","abs":"/abs/B/f","node":"/abs/B","glob":true}],
 "trunc":true}
```

* `mid`: the first 12 hex digits of `<state>/machine-id` (32 hex digits, created once).
* `sid`: `mid:pid:start-epoch-seconds`; `seq` increases by one per record in a session.
* `t`: start time (epoch seconds, 3 decimals); `dur`: seconds; `st`: exit status.
* `cmd`: the command as typed, with ignored path arguments replaced by `<ignored>`, truncated
  to 1024 characters (then `"trunc":true`), and redacted. `glob` and `trunc` are omitted when
  false.
* `paths`: arguments that resolve to existing paths **outside** the cwd (see Capture). The
  record is written to the node of the cwd at the time the command *started*, after it ends.

A directory change writes `{"v":1,"id":…,"k":"cd","t":…,"mid":…,"host":…,"sid":…,"from":"/old",
"to":"/new"}` to the **old** directory's node.

## Privacy floor (all before writing)

1. A command line whose raw text starts with whitespace is not recorded, and neither is the
   directory change it causes, nor any recon it would trigger.
2. Nothing is recorded with the cwd in an ignored subtree; path arguments into ignored subtrees
   are dropped from `paths` and replaced by `<ignored>` in `cmd`.
3. `cmd`, path `arg`s and recon excerpts pass through redaction (`tests/vectors/redact.json`).
4. Session pause/incognito and the global flag `<root>/var/paused` stop all recording.

### Ignore rules

Built in: `~/.ssh ~/.gnupg ~/.password-store $PASSWORD_STORE_DIR ~/.local/share/gopass ~/.aws
~/.config/gcloud ~/.kube ~/.docker` and the root, config and state directories themselves.
Then every `ignore`/`ignore=false` rule from `<config>/policy` and `<config>/policy.d/*.policy`
(see `docs/policy.md`); the last matching rule wins. The adapter re-reads the files when their
mtimes change. Vectors: `tests/vectors/ignore.json`.

## Capture

* Split the command on `| || && ; & |& &! ;; ;& ;|` and `( ) { }`. Skip leading `NAME=value`,
  precommands (`sudo doas command builtin exec nocorrect noglob time nice nohup env` and their
  options) and reserved words.
* Candidates are arguments containing `/`, starting with `~`, or equal to `..`; after a `cd` in
  the same line, any argument. Words containing `$` or a backtick are never evaluated.
* Resolve against the physical cwd (logical for `cd` targets); a glob argument contributes the
  directory before its first wildcard.
* The target node is the path if it is a directory, else its parent. For copy/move
  destinations a non-existent path counts if its parent exists.
* At most 8 candidates are checked per command line; at most 1024 characters are examined.
* Verb table and option handling: see `_dirvana_simple` and `tests/vectors/capture.json`.

## Recon

On entering a directory (not ignored, not skipped), if `%recon.json` is missing, older than a
day, or older than the directory's mtime or the repository's `HEAD`/`config` mtime, refresh it
in the background. Git is run read-only with `GIT_OPTIONAL_LOCKS=0` and
`-c core.fsmonitor=false`; `git status` is never run.

```json
{"v":1,"at":1791416384,"path":"/abs","dir_mtime":1791416384,
 "git":{"toplevel":"/abs","dir":"/abs/.git","branch":"main","remotes":{"origin":"git@…"},
        "root":["<sha>"],"error":null},
 "files":{"README.md":{"size":120,"mtime":1791416000,"excerpt":"first ≤40 lines / 2 KiB"}}}
```

`git` is `null` outside a repository and `{"error":"dubious-ownership"}` (or `"error"`) when
git refuses. `root` is present on the repository's top-level node only; credentials are
stripped from remote URLs. Excerpts are redacted; binary files get an empty excerpt.

## Session commands

`dirvana pause | resume | incognito` act on the current shell; with `--global` they go to the
CLI, which creates or removes `<root>/var/paused`.
