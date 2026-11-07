"""Serve the fixture site in the foreground for manual (headed) captures."""

import argparse
import time

from .server import FixtureSite

parser = argparse.ArgumentParser(prog="python -m fixture_site")
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=8765)
args = parser.parse_args()

with FixtureSite(args.host, args.port) as site:
    print(f"fixture site on {site.url}  (log in with any username/password)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
