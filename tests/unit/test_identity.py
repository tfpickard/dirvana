from __future__ import annotations

import pytest

from dirvana.identity import Identity, best_match, compute, normalize_remote

HOME = "/home/tom"


@pytest.mark.parametrize(
    ("url", "want"),
    [
        ("git@github.com:Owner/Repo.git", "github.com/owner/repo"),
        ("https://github.com/Owner/Repo", "github.com/owner/repo"),
        ("https://tom:pw@GitHub.com/Owner/Repo.git/", "github.com/owner/repo"),
        ("ssh://git@github.com:22/owner/repo.git", "github.com/owner/repo"),
        ("git://git.example.org/Proj/X.git", "git.example.org/Proj/X"),
        ("gitea.local:Team/Thing", "gitea.local/Team/Thing"),
        ("/srv/git/repo.git", None),
        ("../other", None),
        ("file:///srv/git/repo", None),
    ],
)
def test_normalize_remote(url: str, want: str | None) -> None:
    assert normalize_remote(url) == want


def _recon(
    top: str, remotes: dict[str, str] | None = None, root: list[str] | None = None
) -> dict[str, object]:
    git: dict[str, object] = {"toplevel": top, "remotes": remotes or {}}
    if root is not None:
        git["root"] = root
    return {"git": git}


def test_remote_identity_and_aliases() -> None:
    top = f"{HOME}/src/variant-a"
    rec = _recon(top, {"origin": "git@github.com:acme/build.git"}, ["b" * 40, "a" * 40])
    idn = compute(
        f"{top}/tools/ci", _recon(top, {"origin": "git@github.com:acme/build.git"}), rec, HOME
    )
    assert idn.primary == "git-remote:github.com/acme/build:tools/ci"
    assert f"git-root:{'a' * 40}:tools/ci" in idn.aliases
    assert "path:~/src/variant-a/tools/ci" in idn.aliases
    assert "top:variant-a" in idn.aliases


def test_root_identity_without_remote() -> None:
    top = f"{HOME}/notes"
    idn = compute(top, _recon(top, root=["c" * 40]), None, HOME)
    assert idn.primary == f"git-root:{'c' * 40}:"


def test_path_identity_is_portable() -> None:
    assert compute("/Users/tom/x", None, None, "/Users/tom").primary == "path:~/x"
    assert compute("/home/tom/x", None, None, "/home/tom").primary == "path:~/x"
    assert compute("/etc", None, None, HOME).primary == "path:/etc"


def test_upstream_used_when_no_origin() -> None:
    top = f"{HOME}/r"
    idn = compute(
        top, _recon(top, {"upstream": "https://github.com/a/b", "zzz": "x:y/z"}), None, HOME
    )
    assert idn.primary == "git-remote:github.com/a/b:"


def test_sibling_clones_tie_break() -> None:
    """Two clones of one remote share a primary identity; joins must not merge them."""
    prim = "git-remote:github.com/acme/build:"
    a = Identity(prim, ("path:~/src/variant-a", "top:variant-a"))
    b = Identity(prim, ("path:~/src/variant-b", "top:variant-b"))
    local = [("/home/tom/src/variant-a", a), ("/home/tom/src/variant-b", b)]
    foreign_b = Identity(prim, ("path:~/src/variant-b", "top:variant-b"))
    assert best_match(foreign_b, local) == "/home/tom/src/variant-b"
    moved = Identity(prim, ("path:~/elsewhere/variant-a", "top:variant-a"))
    assert best_match(moved, local) == "/home/tom/src/variant-a"
    unknown = Identity(prim, ("path:~/x/clone", "top:clone"))
    assert best_match(unknown, local) is None
    assert best_match(unknown, local[:1]) == "/home/tom/src/variant-a"
