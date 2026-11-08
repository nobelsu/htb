"""Command line interface: ``python -m mapper capture|map|validate``.

Subcommand handlers import their implementation lazily, so ``--help`` and the parser work
without optional heavy dependencies (Playwright browsers, the Anthropic SDK).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from mapper import __version__
from mapper.bundle import RUNS

KINDS = ("read", "write")


def parse_key_value(text: str) -> tuple[str, str]:
    """Parse ``name=value`` (used by ``--marker`` and ``--input``)."""
    name, sep, value = text.partition("=")
    name = name.strip()
    if not sep or not name or not value:
        raise argparse.ArgumentTypeError(f"expected NAME=VALUE, got {text!r}")
    if name.startswith("_"):
        raise argparse.ArgumentTypeError(f"param names may not start with '_' (reserved): {name!r}")
    return name, value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mapper",
        description="Capture a website and map its internal HTTP calls into an action card.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    cap = sub.add_parser("capture", help="record one run of a task into a capture bundle")
    cap.add_argument("start_url", help="page to open first")
    cap.add_argument("--task", required=True, help="task name, e.g. search_companies")
    cap.add_argument("--run", required=True, choices=RUNS, help="which capture run this is")
    cap.add_argument(
        "--marker",
        action="append",
        type=parse_key_value,
        default=[],
        metavar="NAME=VALUE",
        help="marker value for a param (m1/m2 runs); repeatable",
    )
    cap.add_argument(
        "--input",
        action="append",
        type=parse_key_value,
        default=[],
        metavar="NAME=VALUE",
        help="realistic input for a param (real/page2 runs); repeatable",
    )
    cap.add_argument("--kind", choices=KINDS, default="read", help="operator's read/write hint")
    cap.add_argument("--description", default="", help="one-line task description for meta.json")
    cap.add_argument("--state", help="Playwright storage_state to start from (login session)")
    cap.add_argument("--out", default="captures", help="captures root directory (default: captures)")
    cap.add_argument("--headless", action="store_true", help="run headless with a scripted driver")
    cap.add_argument("--script", help="driver script (JSONL of UI steps) for --headless runs")
    cap.add_argument("--force", action="store_true", help="overwrite an existing bundle for this run")
    cap.set_defaults(handler=_cmd_capture)

    mp = sub.add_parser("map", help="turn a domain's capture bundles into a card + mapping report")
    mp.add_argument("captures_dir", help="captures/<domain>/")
    mp.add_argument("--task", help="re-map only this task (used by healing)")
    mp.add_argument("--out", help="output directory (default: the captures dir)")
    mp.add_argument(
        "--live", action="store_true", help="run read examples through the executor (sends real requests)"
    )
    mp.add_argument("--save", action="store_true", help="save the card to the registry if it is valid")
    mp.add_argument("--db", help="registry SQLite file (default: $REGISTRY_DB or ./runtime.db)")
    mp.set_defaults(handler=_cmd_map)

    hl = sub.add_parser(
        "heal", help="re-record the whole site by replaying its recordings, then re-map and save the card"
    )
    hl.add_argument("captures_dir", help="captures/<domain>/")
    hl.add_argument("--task", help="replay only this task (the card is still rebuilt from every task)")
    hl.add_argument("--state", help="login session to replay with (default: each bundle's own state.json)")
    hl.add_argument("--headed", action="store_true", help="show the browser while replaying")
    hl.add_argument("--live", action="store_true", help="run read examples through the executor")
    hl.add_argument("--save", action="store_true", help="save the new card to the registry if it is valid")
    hl.add_argument("--db", help="registry SQLite file (default: $REGISTRY_DB or ./runtime.db)")
    hl.add_argument("--out", help="output directory for card.json/report (default: the captures dir)")
    hl.set_defaults(handler=_cmd_heal)

    ex = sub.add_parser(
        "explore", help="let an LLM pick the site's main tasks, do each live while recorded, then map them"
    )
    ex.add_argument("start_url", help="page to start from (logged in, via --state)")
    ex.add_argument("--state", help="logged-in Playwright storage_state to explore with")
    ex.add_argument("--tasks", nargs="*", help='only these tasks, in plain English (e.g. "search companies")')
    ex.add_argument("--max-tasks", type=int, default=30, help="most tasks the AI lists (default 30)")
    ex.add_argument(
        "--tasks-file",
        help="the actions to map, as a task list (default: mapper/explore/tasks/<domain>.txt if it exists)",
    )
    ex.add_argument("--headed", action="store_true", help="show the browser")
    ex.add_argument(
        "--resurvey", action="store_true", help="redo the look-around even if a recent one is saved"
    )
    ex.add_argument("--parallel", type=int, default=4, help="tasks explored at the same time (default 4)")
    ex.add_argument(
        "--max-browsers", type=int, default=8, help="most browsers open at once, in total (default 8)"
    )
    ex.add_argument("--out", default="captures", help="captures root (default: captures)")
    ex.add_argument(
        "--live", action="store_true", help="run read examples through the executor after mapping"
    )
    ex.add_argument("--save", action="store_true", help="save the card to the registry if it is valid")
    ex.add_argument("--db", help="registry SQLite file (default: $REGISTRY_DB or ./runtime.db)")
    ex.set_defaults(handler=_cmd_explore)

    rc = sub.add_parser("recon", help="guess what a captured site is built with (informational)")
    rc.add_argument("captures_dir", help="captures/<domain>/")
    rc.set_defaults(handler=_cmd_recon)

    val = sub.add_parser("validate", help="run save-time validation on a card")
    val.add_argument("card", help="path/to/card.json")
    val.add_argument("--live", action="store_true", help="run read examples through the executor")
    val.add_argument("--state", help="state.json with the session to use for --live")
    val.set_defaults(handler=_cmd_validate)

    return parser


def _not_ready(name: str) -> int:
    print(f"mapper: '{name}' is not implemented yet", file=sys.stderr)
    return 2


def _cmd_capture(args: argparse.Namespace) -> int:
    try:
        from mapper.capture import record
    except ImportError:
        return _not_ready("capture")
    return record.main(args)


def _cmd_map(args: argparse.Namespace) -> int:
    try:
        from mapper import pipeline
    except ImportError:
        return _not_ready("map")
    return pipeline.main(args)


def _cmd_heal(args: argparse.Namespace) -> int:
    from mapper import pipeline

    return pipeline.heal_main(args)


def _cmd_explore(args: argparse.Namespace) -> int:
    from mapper.explore import explorer

    return explorer.main(args)


def _cmd_recon(args: argparse.Namespace) -> int:
    from mapper import pipeline

    return pipeline.recon_main(args)


def _cmd_validate(args: argparse.Namespace) -> int:
    try:
        from mapper import validate
    except ImportError:
        return _not_ready("validate")
    return validate.main(args)


def load_env() -> None:
    """Read ``.env`` (from the working directory upwards, then the repo root) like the gateway does.
    Variables already set in the environment win."""
    from pathlib import Path

    from dotenv import find_dotenv, load_dotenv

    load_dotenv(find_dotenv(usecwd=True))
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def main(argv: Sequence[str] | None = None) -> int:
    load_env()
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args) or 0)
