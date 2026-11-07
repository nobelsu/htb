"""The HTB Connect browser extension (in /extension): its pinned ID, and adding the hosted gateway's origin.

    uv run python -m runtime.extension --origin https://gateway.example.com   # before loading/publishing
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

EXTENSION_DIR = Path(__file__).resolve().parent.parent / "extension"
MANIFEST = EXTENSION_DIR / "manifest.json"


def extension_id(public_key_b64: str) -> str:
    """Chrome's extension ID: the first 32 hex chars of SHA-256(public key DER), mapped 0-f -> a-p."""
    digest = hashlib.sha256(base64.b64decode(public_key_b64)).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def load_manifest(path: Path = MANIFEST) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def manifest_extension_id(path: Path = MANIFEST) -> str:
    return extension_id(load_manifest(path)["key"])


def add_origin(origin: str, path: Path = MANIFEST) -> list[str]:
    """Allow a gateway origin to talk to the extension (and receive cookies from it)."""
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"not an origin: {origin!r}")
    pattern = f"{parts.scheme}://{parts.hostname}/*"
    manifest = load_manifest(path)
    for field in (manifest["host_permissions"], manifest["externally_connectable"]["matches"]):
        if pattern not in field:
            field.append(pattern)
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    return list(manifest["externally_connectable"]["matches"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure the HTB Connect extension")
    parser.add_argument("--origin", help="gateway origin to allow, e.g. https://gateway.example.com")
    args = parser.parse_args()
    if args.origin:
        print("allowed gateway origins:", ", ".join(add_origin(args.origin)))
    print("extension id:", manifest_extension_id())
    print("load it unpacked from:", EXTENSION_DIR)


if __name__ == "__main__":
    main()
