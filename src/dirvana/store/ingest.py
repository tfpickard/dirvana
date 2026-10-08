"""Ingest: turn raw observations and recon into identity and edge aggregates.

Every step is idempotent and recomputed from canonical inputs, so losing ``var/ingest.json``
only costs a full recompute. Inbound edges are mirrored onto the peer node (``%edges.in.json``)
so either end of an edge can answer "related places" from its own files.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dirvana import identity as ident
from dirvana.paths import (
    EDGES_IN_FILE,
    EDGES_OUT_FILE,
    NODE_FILE,
    OBS_FILE,
    RECON_FILE,
    Dirs,
    real_from_shadow,
    shadow_dir,
)
from dirvana.policy import Rule, is_ignored
from dirvana.store import edges as edgeagg
from dirvana.store.io import as_dict, flock, read_json, read_jsonl, write_json_atomic

_STATE_FILE = "ingest.json"
_INPUTS = (OBS_FILE, RECON_FILE)


@dataclass(slots=True)
class Report:
    scanned: int = 0
    changed: list[str] = field(default_factory=list[str])
    purged: list[str] = field(default_factory=list[str])


def iter_nodes(base: Path) -> Iterator[Path]:
    """Yield every shadow directory under ``base`` that holds node files."""
    stack = [base]
    while stack:
        d = stack.pop()
        has_meta = False
        try:
            entries = list(os.scandir(d))
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            continue
        for e in entries:
            if e.name.startswith("%") and not e.name.startswith("%%"):
                has_meta = True
            elif e.is_dir(follow_symlinks=False):
                stack.append(Path(e.path))
        if has_meta:
            yield d


def _stamp(d: Path) -> list[int]:
    out: list[int] = []
    for name in _INPUTS:
        try:
            st = (d / name).stat()
            out += [st.st_size, st.st_mtime_ns]
        except FileNotFoundError:
            out += [-1, -1]
    return out


class Store:
    """Canonical-store operations over one root."""

    def __init__(self, dirs: Dirs, rules: Sequence[Rule], home_dir: str | None = None) -> None:
        self.dirs = dirs
        self.rules = rules
        self.home_dir = home_dir

    # -- paths ----------------------------------------------------------------------------

    def node_dir(self, real: str) -> Path:
        return shadow_dir(self.dirs.system, real)

    def derived_dir(self, real: str) -> Path:
        return shadow_dir(self.dirs.derived, real)

    def ignored(self, real: str) -> bool:
        return is_ignored(real, self.rules)

    # -- reading --------------------------------------------------------------------------

    def observations(self, real: str) -> list[dict[str, Any]]:
        return list(read_jsonl(self.node_dir(real) / OBS_FILE))

    def recon(self, real: str) -> dict[str, Any] | None:
        return read_json(self.node_dir(real) / RECON_FILE)

    def node_doc(self, real: str) -> dict[str, Any]:
        return read_json(self.node_dir(real) / NODE_FILE) or {}

    def edges_out(self, real: str) -> dict[str, Any] | None:
        return read_json(self.node_dir(real) / EDGES_OUT_FILE)

    def edges_in(self, real: str) -> dict[str, Any] | None:
        return read_json(self.node_dir(real) / EDGES_IN_FILE)

    # -- ingest ---------------------------------------------------------------------------

    def ingest(self, *, full: bool = False) -> Report:
        """Process nodes whose observations or recon changed since the last run."""
        report = Report()
        if not self.dirs.system.is_dir():
            return report
        with flock(self.dirs.run / "ingest.lock"):
            state_path = self.dirs.var / _STATE_FILE
            state: dict[str, Any] = {} if full else (read_json(state_path) or {})
            new_state: dict[str, Any] = {}
            for d in iter_nodes(self.dirs.system):
                real = real_from_shadow(self.dirs.system, d)
                if real is None:
                    continue
                report.scanned += 1
                stamp = _stamp(d)
                if stamp == [-1, -1, -1, -1]:
                    continue  # inbound-only node: nothing of its own to ingest
                key = str(d.relative_to(self.dirs.system))
                if self.ignored(real):
                    self.purge(real)
                    report.purged.append(real)
                    continue
                new_state[key] = stamp
                if state.get(key) != stamp:
                    self._process(real)
                    report.changed.append(real)
            write_json_atomic(state_path, new_state)
        return report

    def _process(self, real: str) -> None:
        d = self.node_dir(real)
        recon = self.recon(real)
        top_recon = None
        top = as_dict((recon or {}).get("git")).get("toplevel")
        if isinstance(top, str) and top != real:
            top_recon = self.recon(top)
        node = self.node_doc(real)
        idn = ident.compute(real, recon, top_recon, self.home_dir)
        node.update({"v": 1, "identity": idn.primary, "aliases": list(idn.aliases)})
        node.setdefault("labels", {})
        node.setdefault("notes", "")
        write_json_atomic(d / NODE_FILE, node)
        self._update_edges(real)

    def _update_edges(self, real: str) -> None:
        d = self.node_dir(real)
        agg = edgeagg.aggregate(
            read_jsonl(d / OBS_FILE),
            self_path=real,
            skip_peer=self.ignored,
        )
        identities = {p: self.node_doc(p).get("identity") for p in agg}
        new_doc = edgeagg.to_document(agg, identities)
        old = edgeagg.entries_by_peer(self.edges_out(real))
        new = edgeagg.entries_by_peer(new_doc)
        if new or old:
            write_json_atomic(d / EDGES_OUT_FILE, new_doc)
        my_identity = self.node_doc(real).get("identity")
        for peer in sorted(set(old) | set(new)):
            if old.get(peer) == new.get(peer) and (self.node_dir(peer) / EDGES_IN_FILE).exists():
                continue
            self._mirror(peer, real, my_identity, new.get(peer))

    def _mirror(
        self, peer: str, source: str, source_identity: object, entry: dict[str, Any] | None
    ) -> None:
        """Replace (or remove) ``source``'s entry in ``peer``'s inbound mirror."""
        path = self.node_dir(peer) / EDGES_IN_FILE
        current = edgeagg.entries_by_peer(read_json(path))
        if entry is None:
            current.pop(source, None)
        else:
            current[source] = {
                "peer": source,
                "peer_identity": source_identity,
                "peer_hint": source.rstrip("/").rsplit("/", 1)[-1] or "/",
                "by_machine": entry["by_machine"],
            }
        if not current:
            if path.exists():
                path.unlink()
            return
        write_json_atomic(path, {"v": 1, "edges": [current[k] for k in sorted(current)]})

    # -- removal --------------------------------------------------------------------------

    def purge(self, real: str) -> None:
        """Delete a node's canonical and derived files and its mirrors on peers."""
        out = edgeagg.entries_by_peer(self.edges_out(real))
        inbound = edgeagg.entries_by_peer(self.edges_in(real))
        for peer in out:
            self._mirror(peer, real, None, None)
        for src in inbound:
            # The source still has an outbound aggregate pointing here; drop it from the
            # source's out file too. Its raw observations are handled by ``forget``.
            path = self.node_dir(src) / EDGES_OUT_FILE
            doc = edgeagg.entries_by_peer(read_json(path))
            if doc.pop(real, None) is not None:
                write_json_atomic(path, {"v": 1, "edges": [doc[k] for k in sorted(doc)]})
        _remove_node_files(self.node_dir(real), self.dirs.system)
        _remove_node_files(self.derived_dir(real), self.dirs.derived)


def _remove_node_files(d: Path, base: Path) -> None:
    """Remove the ``%`` files of one shadow dir, leaving children (other nodes) alone."""
    try:
        entries = list(os.scandir(d))
    except (FileNotFoundError, NotADirectoryError):
        return
    for e in entries:
        if e.name.startswith("%") and not e.name.startswith("%%"):
            if e.is_dir(follow_symlinks=False):
                shutil.rmtree(e.path, ignore_errors=True)
            else:
                Path(e.path).unlink(missing_ok=True)
    _prune_empty(d, base)


def _prune_empty(d: Path, base: Path) -> None:
    """Remove now-empty shadow directories up to (not including) ``base``."""
    while d != base and base in d.parents:
        try:
            d.rmdir()
        except OSError:
            return
        d = d.parent
