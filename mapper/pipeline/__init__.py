"""Mapper pipeline M0–M8: capture bundles → card (see docs/mapper-design.md, "Pipeline")."""

from __future__ import annotations

import argparse
import sys


def main(args: argparse.Namespace) -> int:
    print("mapper: 'map' is not implemented yet (pipeline stages M3+ pending)", file=sys.stderr)
    return 2
