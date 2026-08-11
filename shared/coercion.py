from __future__ import annotations


def optional_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def optional_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def optional_string(value: object) -> str | None:
    return str(value) if value not in (None, "") else None
