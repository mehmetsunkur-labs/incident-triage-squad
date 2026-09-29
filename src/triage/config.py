"""Runtime settings. Values from D1, D2 and D7; the data folder from TRIAGE_DATA_DIR."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REQUIRED_DIRS = ("incidents", "logs", "changes", "runbook")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    out_dir: Path = PROJECT_ROOT / "out"
    prompts_dir: Path = PROJECT_ROOT / "prompts"
    model: str = "claude-opus-5-5"
    specialist_effort: str = "medium"
    synthesis_effort: str = "high"
    max_tool_calls: int = 8
    specialist_timeout_s: float = 90.0
    run_timeout_s: float = 120.0


def resolve_data_dir(value: str | None = None) -> Path:
    """Resolve the data folder; relative paths are taken from the project root."""
    load_dotenv(PROJECT_ROOT / ".env")
    raw = value or os.environ.get("TRIAGE_DATA_DIR")
    if not raw:
        raise ConfigError("TRIAGE_DATA_DIR is not set (see .env.example)")
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path = path.resolve()
    missing = [d for d in REQUIRED_DIRS if not (path / d).is_dir()]
    if missing:
        raise ConfigError(f"data folder {path} is missing: {', '.join(missing)}")
    return path


def load_settings(data_dir: str | None = None, **overrides) -> Settings:
    return Settings(data_dir=resolve_data_dir(data_dir), **overrides)
