"""Command-line entry point.

Kept import-light: subcommand implementations are imported only when they run, so
``dirvana --help`` and the widget clients start fast.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from dirvana._meta import NAME, VERSION

DESCRIPTION = f"""\
{NAME} records what you do in each directory and where you reach from it (edges such as
"from A you diff against B"), keeps that context in a shadow tree, and uses it to suggest
commands. It never writes inside the directories it observes."""

EPILOG = f"""\
Session-scoped commands (pause, resume, incognito without --global) are handled by the zsh
plugin's `{NAME}` shell function. See {NAME}(1) for files and environment variables."""


def _dir_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("dir", nargs="?", default=".", help="directory (default: current)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=NAME,
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{NAME} {VERSION}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    p = sub.add_parser("status", help="show where data lives and how much there is")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser("show", help="show what is known about a directory")
    _dir_arg(p)
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser("edges", help="list a directory's outbound and inbound edges")
    _dir_arg(p)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--in", dest="direction", action="store_const", const="in", help="inbound only")
    g.add_argument(
        "--out", dest="direction", action="store_const", const="out", help="outbound only"
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser("path", help="print the shadow-tree path of a directory's node")
    _dir_arg(p)
    p.add_argument("--derived", action="store_true", help="print the derived (LLM cache) path")

    p = sub.add_parser("forget", help="delete what is known about a directory")
    p.add_argument("dir", help="directory to forget")
    p.add_argument("-r", "--recursive", action="store_true", help="forget the whole subtree")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")

    p = sub.add_parser("note", help="attach a free-text note to a directory")
    p.add_argument("dir", help="directory")
    p.add_argument("text", nargs="+", help="note text (empty string clears it)")

    p = sub.add_parser("label", help="set labels (key=value; key= removes) on a directory")
    p.add_argument("dir", help="directory")
    p.add_argument("labels", nargs="+", metavar="KEY=VALUE")

    for name, text in (
        ("pause", "stop recording in all shells (with --global)"),
        ("resume", "resume recording in all shells (with --global)"),
        ("incognito", "stop recording in all shells (with --global)"),
    ):
        p = sub.add_parser(name, help=text)
        p.add_argument("--global", dest="global_", action="store_true", help="affect every shell")

    p = sub.add_parser("policy", help="inspect and edit per-subtree policy")
    psub = p.add_subparsers(dest="policy_command", metavar="ACTION", required=True)
    q = psub.add_parser("explain", help="show the effective policy for a directory and why")
    _dir_arg(q)
    psub.add_parser("check", help="validate all policy files")
    q = psub.add_parser("edit", help="open the policy file for a subtree (or the global one)")
    q.add_argument("dir", nargs="?", help="subtree root (default: the global policy file)")

    p = sub.add_parser("ingest", help="fold new observations into edges and identities now")
    p.add_argument("--full", action="store_true", help="recompute every node")

    p = sub.add_parser("enrich", help="build derived (LLM) context for directories now")
    p.add_argument("dirs", nargs="*", metavar="DIR", help="directories (default: every dirty node)")
    p.add_argument("--force", action="store_true", help="regenerate even if nothing changed")
    p.add_argument(
        "--dry-run", action="store_true", help="list what would be enriched; call nothing"
    )

    p = sub.add_parser("daemon", help="run the background daemon (ingest, enrich, serve hotkeys)")
    p.add_argument("--oneshot", action="store_true", help="run one tick and exit (for timers)")

    p = sub.add_parser("suggest", help="hotkey client: candidate commands for a directory")
    p.add_argument("--format", choices=["zsh", "tsv"], default="tsv", help="output format")
    p.add_argument("-n", type=int, help="number of candidates")
    p = sub.add_parser("brief", help="hotkey client: a briefing for a directory")

    p = sub.add_parser("doctor", help="check the installation, daemon and providers")
    p.add_argument("--providers", action="store_true", help="also contact each provider")

    p = sub.add_parser("smoke", help="make one tiny real call per configured provider")
    p.add_argument("--provider", help="only this provider instance")

    p = sub.add_parser("init", help="print the line that loads the shell plugin")
    p.add_argument("shell", choices=["zsh"], help="shell to integrate with")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    from dirvana.cli import commands

    handler = getattr(commands, "cmd_" + args.command.replace("-", "_"))
    try:
        rc: int = handler(args)
    except commands.CliError as e:
        print(f"{NAME}: {e}", file=sys.stderr)
        return e.code
    except BrokenPipeError:
        return 0
    return rc


if __name__ == "__main__":
    sys.exit(main())
