"""JSONPath helpers. All paths we emit must compile with ``jsonpath_ng.ext.parse`` (spec: Extraction)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from functools import lru_cache
from typing import Any

from jsonpath_ng.ext import parse as _parse

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED = frozenset({"where", "wherenot"})  # jsonpath_ng lexer keywords

Scalar = str | int | float | bool | None


def key_segment(key: str) -> str:
    if _IDENT.match(key) and key not in _RESERVED:
        return f".{key}"
    escaped = key.replace("\\", "\\\\").replace("'", "\\'")
    return f"['{escaped}']"


def child(path: str, key: str | int) -> str:
    return f"{path}[{key}]" if isinstance(key, int) else path + key_segment(key)


def flatten(obj: Any, root: str = "$") -> Iterator[tuple[str, Any]]:
    """Yield ``(jsonpath, leaf)`` for every scalar leaf, and for empty lists/objects."""
    if isinstance(obj, dict):
        if not obj:
            yield root, obj
        for k, v in obj.items():
            yield from flatten(v, child(root, str(k)))
    elif isinstance(obj, list):
        if not obj:
            yield root, obj
        for i, v in enumerate(obj):
            yield from flatten(v, child(root, i))
    else:
        yield root, obj


def map_leaves(obj: Any, fn, root: str = "$") -> Any:
    """Rebuild ``obj`` with each leaf (as yielded by ``flatten``) replaced by ``fn(path, leaf)``."""
    if isinstance(obj, dict) and obj:
        return {k: map_leaves(v, fn, child(root, str(k))) for k, v in obj.items()}
    if isinstance(obj, list) and obj:
        return [map_leaves(v, fn, child(root, i)) for i, v in enumerate(obj)]
    return fn(root, obj)


def walk(obj: Any, root: str = "$") -> Iterator[tuple[str, Any]]:
    """Yield ``(jsonpath, node)`` for every node, containers included (pre-order)."""
    yield root, obj
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk(v, child(root, str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, child(root, i))


@lru_cache(maxsize=4096)
def compile_path(expr: str):
    return _parse(expr)


def find(expr: str, obj: Any) -> list[Any]:
    return [m.value for m in compile_path(expr).find(obj)]


def compiles(expr: str) -> bool:
    try:
        compile_path(expr)
    except Exception:
        return False
    return True
