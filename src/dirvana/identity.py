"""Node identity: the join key between machines.

Precedence: ``git-remote:<normalized remote>:<repo-relative path>``, then
``git-root:<root commit>:<repo-relative path>`` for remoteless repos, then ``path:<~-portable
path>``. Identity is *not* unique: sibling clones of one remote share it, so joins tie-break on
the ``path:`` and ``top:`` aliases (see :func:`best_match`).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from dirvana.paths import portable
from dirvana.store.io import as_dict, as_list

_CASE_INSENSITIVE_HOSTS: Final = frozenset({"github.com", "gitlab.com", "bitbucket.org"})
_SCP_LIKE = re.compile(r"^(?:[^@/]+@)?([^:/]+):(?!//)(.+)$")
_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/]*@)?([^/:]+)(?::\d+)?(/.*)?$")


def normalize_remote(url: str) -> str | None:
    """``git@GitHub.com:Owner/Repo.git`` -> ``github.com/owner/repo``. ``None`` for local paths."""
    url = url.strip()
    m = _URL.match(url)
    if m:
        host, path = m.group(1), m.group(2) or ""
    elif url.startswith(("/", ".", "file:")):
        return None
    else:
        m = _SCP_LIKE.match(url)
        if not m:
            return None
        host, path = m.group(1), m.group(2)
    host = host.lower()
    path = path.strip("/")
    path = path.removesuffix(".git").rstrip("/")
    if not path:
        return None
    if host in _CASE_INSENSITIVE_HOSTS:
        path = path.lower()
    return f"{host}/{path}"


def pick_remote(remotes: Mapping[str, str]) -> str | None:
    for name in ("origin", "upstream", *sorted(remotes)):
        url = remotes.get(name)
        if url:
            norm = normalize_remote(url)
            if norm:
                return norm
    return None


@dataclass(frozen=True, slots=True)
class Identity:
    primary: str
    aliases: tuple[str, ...]

    def all_keys(self) -> tuple[str, ...]:
        return (self.primary, *self.aliases)


def _rel(path: str, top: str) -> str:
    if path == top:
        return ""
    return path[len(top) :].lstrip("/") if path.startswith(top.rstrip("/") + "/") else ""


def compute(
    path: str,
    recon: Mapping[str, Any] | None,
    top_recon: Mapping[str, Any] | None,
    home_dir: str | None = None,
) -> Identity:
    """Compute identity for ``path`` from its recon and its repo toplevel's recon."""
    path_key = f"path:{portable(path, home_dir)}"
    git = as_dict((recon or {}).get("git"))
    top = git.get("toplevel")
    if not isinstance(top, str) or not top:
        return Identity(path_key, ())
    rel = _rel(path, top)
    top_git = as_dict((top_recon or {}).get("git")) or git
    remotes_raw = as_dict(git.get("remotes")) or as_dict(top_git.get("remotes"))
    remotes = {str(k): str(v) for k, v in remotes_raw.items()}
    roots = sorted(str(r) for r in as_list(top_git.get("root")) or as_list(git.get("root")))
    aliases: list[str] = []
    remote = pick_remote(remotes)
    root_key = f"git-root:{roots[0]}:{rel}" if roots else None
    top_key = "top:" + top.rstrip("/").rsplit("/", 1)[-1]
    if remote:
        primary = f"git-remote:{remote}:{rel}"
        if root_key:
            aliases.append(root_key)
    elif root_key:
        primary = root_key
    else:
        primary = path_key
    if primary != path_key:
        aliases.append(path_key)
    aliases.append(top_key)
    return Identity(primary, tuple(aliases))


def best_match(foreign: Identity, candidates: Sequence[tuple[str, Identity]]) -> str | None:
    """Pick the local node (by path) a foreign node joins to, or ``None`` if ambiguous.

    Candidates must already share ``foreign.primary``. Ties break on the ``path:`` alias, then
    the ``top:`` alias; anything still ambiguous does not join.
    """
    same = [(p, i) for p, i in candidates if i.primary == foreign.primary]
    if len(same) <= 1:
        return same[0][0] if same else None
    for prefix in ("path:", "top:"):
        want = [a for a in foreign.aliases if a.startswith(prefix)]
        if not want:
            continue
        hits = [(p, i) for p, i in same if want[0] in i.all_keys()]
        if len(hits) == 1:
            return hits[0][0]
        if hits:
            same = hits
    return None
