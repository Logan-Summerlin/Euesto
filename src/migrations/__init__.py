"""Ordered SQLite schema migrations for local desktop state."""

from .versions import CURRENT_SCHEMA_VERSION, migrate

__all__ = ["CURRENT_SCHEMA_VERSION", "migrate"]
