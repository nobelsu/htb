"""Check a card file against CARD_SPEC.md's save-time rules, without running anything.

    uv run python -m spider.card.check cards/linkedin.com.json

Prints one line per problem (exit code 1), or "OK: no issues". This is `spider.card.validate_card`, the same
check the registry runs before saving; `harmap/validate.py` calls this script and reads its output.
"""

import json
import sys

from spider.card import validate_card


def main(path: str) -> int:
    try:
        with open(path) as fh:
            card = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        print(f"can't read {path}: {e}")
        return 1
    report = validate_card(card)
    for error in report.errors:
        print(error)
    if report.errors:
        return 1
    print("OK: no issues")  # warnings are left out: harmap/validate.py reads any other line as an error
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
