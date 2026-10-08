from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dirvana.paths import (
    EDGES_IN_FILE,
    EDGES_OUT_FILE,
    OBS_FILE,
    Dirs,
    real_from_shadow,
    shadow_dir,
)
from dirvana.policy import load_rules
from dirvana.store.forget import forget
from dirvana.store.ingest import Store
from dirvana.store.io import read_json

T0 = 1_791_400_000.0
A = "/home/tom/src/a"
B = "/home/tom/src/b"
C = "/home/tom/src/c"


def _store(tmp_path: Path) -> Store:
    dirs = Dirs(tmp_path / "root", tmp_path / "cfg", tmp_path / "state")
    dirs.ensure_root()
    return Store(dirs, load_rules(dirs, "/home/tom"), "/home/tom")


def _cmd(
    seq: int, cmd: str, paths: list[tuple[str, str, str]], sid: str = "m:1:1"
) -> dict[str, Any]:
    return {
        "v": 1,
        "id": f"{sid}:{seq}",
        "k": "cmd",
        "t": T0 + seq,
        "mid": "m",
        "host": "h",
        "sid": sid,
        "cwd": A,
        "cmd": cmd,
        "st": 0,
        "dur": 0.01,
        "paths": [
            {"verb": v, "arg": a, "abs": ab, "node": ab if ab in (B, C) else ab.rsplit("/", 1)[0]}
            for v, a, ab in paths
        ],
    }


def _append(store: Store, real: str, *records: dict[str, Any], raw: bytes = b"") -> None:
    d = store.node_dir(real)
    d.mkdir(parents=True, exist_ok=True)
    with (d / OBS_FILE).open("ab") as f:
        for r in records:
            f.write(json.dumps(r).encode() + b"\n")
        f.write(raw)


def test_shadow_roundtrip(tmp_path: Path) -> None:
    base = tmp_path / "system"
    for p in ("/", "/etc", "/a/%b/%%c/d%", "/x y/z"):
        assert real_from_shadow(base, shadow_dir(base, p)) == p
    assert real_from_shadow(base, base / "a" / "%obs.jsonl") is None


def test_ingest_builds_outbound_and_inbound(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _append(
        store,
        A,
        _cmd(1, "ls ../b", [("list", "../b", B)]),
        _cmd(2, "cp ../b/x .", [("copy-from", "../b/x", f"{B}/x")]),
        _cmd(3, "diff ../b/f f", [("diff", "../b/f", f"{B}/f")], sid="m:2:2"),
        {
            "v": 1,
            "id": "m:2:2:4",
            "k": "cd",
            "t": T0 + 4,
            "mid": "m",
            "sid": "m:2:2",
            "from": A,
            "to": B,
        },
    )
    report = store.ingest()
    assert report.changed == [A]
    out = read_json(store.node_dir(A) / EDGES_OUT_FILE)
    assert out is not None
    [edge] = out["edges"]
    assert edge["peer"] == B
    verbs = edge["by_machine"]["m"]["verbs"]
    assert set(verbs) == {"list", "copy-from", "diff", "cd"}
    assert sorted(edge["by_machine"]["m"]["examples"]) == ["f", "x"]
    inbound = read_json(store.node_dir(B) / EDGES_IN_FILE)
    assert inbound is not None
    assert [e["peer"] for e in inbound["edges"]] == [A]
    assert inbound["edges"][0]["by_machine"] == edge["by_machine"]
    node = read_json(store.node_dir(A) / "%node.json")
    assert node is not None
    assert node["identity"] == "path:~/src/a"


def test_ingest_is_incremental_and_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _append(store, A, _cmd(1, "ls ../b", [("list", "../b", B)]))
    store.ingest()
    snapshot = (store.node_dir(B) / EDGES_IN_FILE).read_bytes()
    assert store.ingest().changed == []
    store.ingest(full=True)
    assert (store.node_dir(B) / EDGES_IN_FILE).read_bytes() == snapshot
    (store.dirs.var / "ingest.json").unlink()
    store.ingest()
    assert (store.node_dir(B) / EDGES_IN_FILE).read_bytes() == snapshot


def test_torn_and_binary_lines_are_tolerated(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rec = _cmd(1, "cat ../b/\udcff", [("read", "../b/x", f"{B}/x")])
    line = json.dumps(rec, ensure_ascii=False).encode("utf-8", errors="surrogateescape")
    _append(store, A, raw=line + b"\n" + b"not json\n" + b'{"v":1,"k":"cmd","t":')
    store.ingest()
    assert len(store.observations(A)) == 1
    assert (store.node_dir(B) / EDGES_IN_FILE).exists()


def test_ignored_peers_and_nodes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _append(store, A, _cmd(1, "cat ~/.ssh/x", [("read", "~/.ssh/x", "/home/tom/.ssh/x")]))
    store.ingest()
    assert not (store.node_dir(A) / EDGES_OUT_FILE).exists()
    _append(store, "/home/tom/.gnupg", _cmd(1, "ls", []))
    report = store.ingest()
    assert report.purged == ["/home/tom/.gnupg"]
    assert not store.node_dir("/home/tom/.gnupg").exists()


def test_forget_scrubs_references(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _append(
        store,
        A,
        _cmd(1, "ls ../b", [("list", "../b", B)]),
        _cmd(2, "ls ../c", [("list", "../c", C)]),
        {
            "v": 1,
            "id": "m:1:1:3",
            "k": "cd",
            "t": T0 + 3,
            "mid": "m",
            "sid": "m:1:1",
            "from": A,
            "to": B,
        },
    )
    _append(store, B, _cmd(1, "make", []))
    store.ingest()
    removed = forget(store, B)
    assert removed == [B]
    assert not store.node_dir(B).exists()
    peers = [e["peer"] for e in (read_json(store.node_dir(A) / EDGES_OUT_FILE) or {})["edges"]]
    assert peers == [C]
    cmds = [o.get("cmd") for o in store.observations(A)]
    assert "ls <forgotten>" in cmds
    assert all(o.get("k") != "cd" for o in store.observations(A))
    # A full recompute must not resurrect the edge.
    store.ingest(full=True)
    assert [e["peer"] for e in (read_json(store.node_dir(A) / EDGES_OUT_FILE) or {})["edges"]] == [
        C
    ]
