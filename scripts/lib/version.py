"""Версия плагина и корень установленной копии."""

from __future__ import annotations

from pathlib import Path

__version__ = "0.1.14"


def plugin_root() -> Path:
    """Корень плагина: папка с `.claude-plugin/` (на два уровня выше `scripts/lib/`)."""
    return Path(__file__).resolve().parents[2]
