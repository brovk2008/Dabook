"""
Config loader — reads dabook.toml (if present), applies profile, merges CLI overrides.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dabook.config.models import PROFILES, DabookConfig


def load_config(
    workspace_or_toml: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> DabookConfig:
    """
    Load configuration from ``dabook.toml`` if it exists, then apply overrides.

    ``workspace_or_toml`` can be a directory (will look for ``dabook.toml`` inside)
    or a direct path to a TOML file.
    """
    raw: dict[str, Any] = {}

    # Find and parse TOML
    toml_path: Path | None = None
    if workspace_or_toml is not None:
        p = Path(workspace_or_toml)
        if p.is_dir():
            candidate = p / "dabook.toml"
            if candidate.is_file():
                toml_path = candidate
        elif p.is_file() and p.suffix == ".toml":
            toml_path = p

    if toml_path and toml_path.is_file():
        try:
            import tomllib  # Python 3.11+
        except ImportError:
            import tomli as tomllib  # type: ignore[no-redef]
        with open(toml_path, "rb") as f:
            raw = tomllib.load(f)

    # Apply profile preset on top of raw
    profile_name = (overrides or {}).get("profile") or raw.get("profile", "balanced")
    preset = PROFILES.get(str(profile_name), {})
    raw = _deep_merge(raw, preset)

    # Apply CLI overrides
    if overrides:
        raw = _deep_merge(raw, overrides)

    return DabookConfig.model_validate(raw)


def save_config(cfg: DabookConfig, path: Path) -> None:
    """Write the current config as a TOML snapshot (for reproducibility)."""
    try:
        import tomli_w
    except ImportError:
        # Fallback: write as JSON with .toml extension (not ideal but functional)
        import json

        path.write_text(json.dumps(cfg.model_dump(mode="json"), indent=2))
        return

    data = cfg.model_dump(mode="json")
    with open(path, "wb") as f:
        tomli_w.dump(data, f)


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result
