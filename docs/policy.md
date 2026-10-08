# Policy reference

Policy is user intent, so it lives in the config directory (`~/.config/dirvana` by default),
never in the data root: it survives `rm -rf <root>` and `dirvana forget`, and it can live in
your dotfiles.

## Files

* `~/.config/dirvana/policy`: the global file. Patterns are absolute (`/tmp`) or home-relative
  (`~/work`); a bare name (`secrets`) matches at any depth.
* `~/.config/dirvana/policy.d/*.policy`: per-subtree files. The first non-comment line is
  `@root DIR`; patterns below are relative to DIR, with gitignore semantics.

`dirvana policy edit` opens the global file; `dirvana policy edit DIR` opens (creating if
needed) the per-subtree file rooted at DIR. `dirvana policy check` validates everything.

```
# ~/.config/dirvana/policy                 # ~/.config/dirvana/policy.d/acme.policy
/tmp/**          retention=ephemeral ttl=7d  @root ~/work/acme
~/scratch        llm=none                    **         llm=copilot
**/secrets/**    ignore                      oss/**     llm=anthropic,openai
                                             vendor     ignore
```

## Rules

`PATTERN key[=value] ...`; `#` starts a comment.

| Key | Values | Meaning |
|---|---|---|
| `ignore` | `ignore`, `ignore=true`, `ignore=false` | Never record anything here. Enforced by the shell hook before storage. |
| `retention` | `eternal` (default), `ephemeral` | Ephemeral nodes are deleted `ttl` after their last activity. |
| `ttl` | duration: `90s 15m 6h 7d 2w 1d12h` | Used with `retention=ephemeral`. |
| `enrich` | `async` (default), `sync`, `off` | When derived (LLM) context is built. |
| `llm` | `none` or `name[,name...]` | Which provider instances may receive this subtree's data. |

## Matching

* `*`, `?` and `[...]` (`[!...]` negates) match within one path component; `**` spans any
  number of components.
* A leading `/` anchors to the file's base; a pattern containing `/` is anchored too; a bare
  name matches at any depth below the base.
* A rule that matches a directory applies to that directory **and everything below it**, so
  `~/work/acme` and `~/work/acme/**` mean the same thing. (Unlike gitignore, `X/**` includes X:
  a pin must never miss the subtree's root.)

## Precedence

Field by field, the last setter wins, in this order: built-in defaults, the global file, then
`policy.d` files from the shallowest `@root` to the deepest (ties by file name in byte order),
lines in file order. `dirvana policy explain DIR` prints each effective value and the
`file:line` that set it.

Built-in defaults: `/tmp`, `/private/tmp`, `/var/tmp` and `/private/var/folders` are
`retention=ephemeral ttl=7d`; secret stores (`~/.ssh`, `~/.gnupg`, password stores, cloud
credential dirs) and dirvana's own directories are `ignore`. A local `ignore=false` can lift a
built-in ignore; imported layers cannot.
