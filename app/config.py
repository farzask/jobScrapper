"""Config loading. One YAML file is the single source of truth for tuning."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


class Config:
    def __init__(self, data: dict[str, Any]):
        self._d = data

    def __getitem__(self, key: str) -> Any:
        return self._d[key]

    def get(self, path: str, default: Any = None) -> Any:
        """Dotted lookup: cfg.get('search.max_age_days', 30)."""
        cur: Any = self._d
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def path(self, key: str) -> Path:
        """Resolve a configured path relative to the project root."""
        p = Path(self.get(f"paths.{key}", key))
        return p if p.is_absolute() else ROOT / p

    @property
    def anthropic_key(self) -> str | None:
        return os.environ.get("ANTHROPIC_API_KEY") or None


@lru_cache(maxsize=1)
def load(config_path: str | None = None) -> Config:
    p = Path(config_path) if config_path else ROOT / "config.yaml"
    with open(p, encoding="utf-8") as fh:
        return Config(yaml.safe_load(fh) or {})
