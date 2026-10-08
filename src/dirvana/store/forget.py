"""Forget a directory (or subtree): its node, its derived context, and references to it."""

from __future__ import annotations

from typing import Any

from dirvana.paths import real_from_shadow
from dirvana.store import edges as edgeagg
from dirvana.store.ingest import Store, iter_nodes
from dirvana.store.io import as_dict, as_list
from dirvana.store.rewrite import rewrite_observations

FORGOTTEN = "<forgotten>"


def _under(path: str, target: str, recursive: bool) -> bool:
    return path == target or (recursive and path.startswith(target.rstrip("/") + "/"))


def forget(store: Store, target: str, *, recursive: bool = False) -> list[str]:
    """Remove ``target`` (and descendants with ``recursive``). Returns the nodes removed.

    Policy is untouched: it lives in config. Forgetting is not ignoring; visit the directory
    again and recording resumes.
    """
    store.ingest()
    if recursive:
        base = store.node_dir(target)
        victims = sorted(
            r for d in iter_nodes(base) if (r := real_from_shadow(store.dirs.system, d)) is not None
        )
    else:
        victims = [target] if store.node_dir(target).is_dir() else []

    # Sources that recorded edges into any victim must lose those references, or the next
    # ingest would resurrect the edge from their raw observations.
    sources: set[str] = set()
    for v in victims:
        sources.update(edgeagg.entries_by_peer(store.edges_in(v)))
    sources.difference_update(s for s in list(sources) if _under(s, target, recursive))

    def scrub(rec: dict[str, Any]) -> dict[str, Any] | None:
        if rec.get("k") == "cd":
            return None if _under(str(rec.get("to", "")), target, recursive) else rec
        paths = as_list(rec.get("paths"))
        keep: list[Any] = []
        cmd = str(rec.get("cmd", ""))
        for raw in paths:
            p = as_dict(raw)
            if p and _under(str(p.get("node", "")), target, recursive):
                arg = str(p.get("arg", ""))
                if arg:
                    cmd = cmd.replace(arg, FORGOTTEN)
            else:
                keep.append(raw)
        if len(keep) != len(paths):
            rec = {**rec, "paths": keep, "cmd": cmd}
        return rec

    for s in sorted(sources):
        rewrite_observations(store.node_dir(s), scrub)
    for v in victims:
        store.purge(v)
    store.ingest()
    return victims
