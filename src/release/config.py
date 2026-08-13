"""Loads config.toml from the repo root."""

from __future__ import annotations

import tomllib
from pathlib import Path

_DEFAULT = Path(__file__).resolve().parents[2] / "config.toml"


def load(path: str | Path | None = None) -> dict:
    return tomllib.loads(Path(path or _DEFAULT).read_text())
