"""Command line interface: ``python -m mapper capture|map|validate``.

Subcommand handlers import their implementation lazily, so ``--help`` and the parser work
without optional heavy dependencies (Playwright browsers, the Anthropic SDK).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from mapper import __version__

RUNS = ("m1", "m2", "real", "page2")
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
    cap.set_defaults(handler=_cmd_capture)

    mp = sub.add_parser("map", help="turn a domain's capture bundles into a card + mapping report")
    mp.add_argument("captures_dir", help="captures/<domain>/")
    mp.add_argument("--task", help="re-map only this task (used by healing)")
    mp.add_argument("--out", help="output directory (default: the captures dir)")
    mp.set_defaults(handler=_cmd_map)

    val = sub.add_parser("validate", help="run save-time validation on a card")
    val.add_argument("card", help="path/to/card.json")
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


def _cmd_validate(args: argparse.Namespace) -> int:
    try:
        from mapper import validate
    except ImportError:
        return _not_ready("validate")
    return validate.main(args)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args) or 0)
