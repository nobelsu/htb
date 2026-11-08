"""Agent-input validation against an action's `params`, plus the JSON Schema for MCP."""

from __future__ import annotations

from typing import Any

from schema.card import Action, Param

CONFIRMATION_ARG = "_confirmation_id"


def _type_error(spec: Param, value: Any) -> str | None:
    t = spec.type
    if t == "string":
        ok = isinstance(value, str)
    elif t == "integer":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif t == "number":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif t == "boolean":
        ok = isinstance(value, bool)
    else:  # enum
        if value not in (spec.values or []):
            return f"must be one of {spec.values}"
        return None
    return None if ok else f"must be {'an' if t == 'integer' else 'a'} {t}"


def coerce_params(specs: dict[str, Param], given: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate `given` and apply defaults.

    Returns (params, field_errors). `params` excludes `_`-prefixed keys and
    optional params with no value and no default.
    """
    errors: dict[str, str] = {}
    out: dict[str, Any] = {}
    for key in given:
        if key.startswith("_"):
            if key != CONFIRMATION_ARG:
                errors[key] = "reserved name"
        elif key not in specs:
            errors[key] = "unknown param"
    for name, spec in specs.items():
        value = given.get(name)
        if value is None:
            if spec.required:
                errors[name] = "required"
            elif spec.has_default:
                out[name] = spec.default
            continue
        err = _type_error(spec, value)
        if err:
            errors[name] = err
        else:
            out[name] = value
    return out, errors


def format_field_errors(errors: dict[str, str]) -> str:
    return "; ".join(f"{k}: {v}" for k, v in sorted(errors.items()))


SPEND_PREFIX = "This will spend money. "


def tool_description(action: Action) -> str:
    """The MCP tool description: the card's description, plus confirmation instructions when needed."""
    text = action.description
    if action.spends:
        text += " This action spends money."
    if action.requires_confirmation:
        text += (
            " Requires the user's confirmation: the first call sends nothing and returns NEEDS_CONFIRMATION "
            "with a summary. Show that summary to the user and wait for their explicit approval before "
            "calling again with _confirmation_id. Their original request is not approval."
        )
    return text


def confirmation_summary(action: Action, canonical: str) -> str:
    """The summary shown to the user (spec: the action description plus the params)."""
    prefix = SPEND_PREFIX if action.spends else ""
    return f"{prefix}{action.description} Params: {canonical}"


_JSON_TYPES = {"string": "string", "integer": "integer", "number": "number", "boolean": "boolean"}


def input_schema(specs: dict[str, Param], requires_confirmation: bool) -> dict[str, Any]:
    """JSON Schema for the MCP tool's input."""
    props: dict[str, Any] = {}
    for name, spec in specs.items():
        prop: dict[str, Any] = {}
        if spec.type == "enum":
            prop["enum"] = list(spec.values or [])
        else:
            prop["type"] = _JSON_TYPES[spec.type]
        if spec.description:
            prop["description"] = spec.description
        if spec.has_default:
            prop["default"] = spec.default
        if spec.example is not None:
            prop["examples"] = [spec.example]
        props[name] = prop
    if requires_confirmation:
        props[CONFIRMATION_ARG] = {
            "type": "string",
            "description": "Confirmation id from a previous NEEDS_CONFIRMATION result. Only pass it after "
            "the user has seen that result's summary and explicitly approved it; then call again with "
            "identical params plus this id. Never pass it on your own judgement.",
        }
    schema: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    required = [n for n, s in specs.items() if s.required]
    if required:
        schema["required"] = required
    return schema
