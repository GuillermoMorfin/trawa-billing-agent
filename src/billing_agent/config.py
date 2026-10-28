"""Paths and settings, read from the environment with sensible defaults."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DB_PATH = Path("data/billing.duckdb")


def db_path() -> Path:
    return Path(os.environ.get("BILLING_DB_PATH", DEFAULT_DB_PATH))
