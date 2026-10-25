"""One-line corrective checks of model tool arguments against the offered schemas (3.5).

Smaller models often send malformed JSON or arguments of the wrong shape. The check runs
before any permission prompt or executor call and names the first offending field and the
expected type, so the model can fix the call in one step. It understands the subset of JSON
Schema the tool definitions use; the executor still validates everything itself.
"""

from __future__ import annotations

import json
from typing import Any

JSON_TYPES = {
    "string": str,
    "boolean": bool,
    "object": dict,
    "array": list,
}


class ArgumentError(ValueError):
    """A tool call whose arguments cannot be used; the message is one corrective line."""


def parse_call_arguments(name: str, raw_arguments: str) -> dict[str, Any]:
    """Decode a tool call's JSON arguments, or explain how they are malformed."""
    try:
        arguments = json.loads(raw_arguments or "{}")
    except json.JSONDecodeError as exc:
        raise ArgumentError(
            f"{name} arguments are not valid JSON ({exc.msg} at character {exc.pos}); send "
            "one JSON object"
        ) from None
    if not isinstance(arguments, dict):
        raise ArgumentError(f"{name} arguments must be a JSON object")
    return arguments


def validate_arguments(name: str, schema: dict[str, Any], arguments: dict[str, Any]) -> None:
    """Raise :class:`ArgumentError` naming the first field that violates ``schema``."""
    problem = _check(schema, arguments, "")
    if problem:
        raise ArgumentError(f"{name}: {problem}")


def _check(schema: dict[str, Any], value: Any, path: str) -> str | None:
    label = path or "arguments"
    expected = schema.get("type")
    if expected is not None and not _matches_type(value, expected):
        return f"{label} must be {_describe(expected)}, got {_kind(value)}"
    if "enum" in schema and value not in schema["enum"]:
        options = ", ".join(str(item) for item in schema["enum"])
        return f"{label} must be one of {options}"
    if isinstance(value, str):
        if len(value) < int(schema.get("minLength", 0)):
            return f"{label} must not be empty"
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            return f"{label} must be at most {schema['maxLength']} characters"
    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            return f"{label} must be at least {schema['minimum']}"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{label} must be at most {schema['maximum']}"
    if isinstance(value, list):
        if len(value) < int(schema.get("minItems", 0)):
            return f"{label} needs at least {schema['minItems']} item(s)"
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            return f"{label} allows at most {schema['maxItems']} items"
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                problem = _check(items, item, f"{label}[{index}]")
                if problem:
                    return problem
    if isinstance(value, dict) and (schema.get("properties") or schema.get("required")):
        properties = schema.get("properties") or {}
        for field in schema.get("required") or ():
            if field not in value:
                return f"missing required field {_join(path, field)}"
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                allowed = ", ".join(properties)
                return f"unknown field {_join(path, unknown[0])}; allowed: {allowed}"
        for field, item in value.items():
            if field in properties:
                problem = _check(properties[field], item, _join(path, field))
                if problem:
                    return problem
    return None


def _matches_type(value: Any, expected: str | list[str]) -> bool:
    if isinstance(expected, list):
        return any(_matches_type(value, item) for item in expected)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    kind = JSON_TYPES.get(expected)
    return kind is None or isinstance(value, kind)


def _describe(expected: str | list[str]) -> str:
    if isinstance(expected, list):
        return " or ".join(_describe(item) for item in expected)
    return {"integer": "an integer", "array": "an array", "object": "an object"}.get(
        expected, f"a {expected}"
    )


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, int):
        return "an integer"
    if isinstance(value, float):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "an array"
    return "an object"


def _join(path: str, field: str) -> str:
    return f"{path}.{field}" if path else field
