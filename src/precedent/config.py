from __future__ import annotations

import os
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator

_RATE_PATTERN = re.compile(r"^\d+(\.\d+)?$")


class ConfigError(ValueError):
    """Invalid local configuration. Safe to print; never includes secrets."""


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_root: Path
    anthropic_api_key: str | None = Field(default=None, repr=False)
    model: str | None = None
    db_path: Path
    data_dir: Path
    max_model_calls: StrictInt = Field(gt=0)
    max_tool_calls: StrictInt = Field(gt=0)
    max_case_seconds: StrictInt = Field(gt=0)
    request_timeout_seconds: StrictInt = Field(gt=0)
    max_output_tokens: StrictInt = Field(gt=0)
    input_usd_per_million: Decimal | None = None
    output_usd_per_million: Decimal | None = None
    enable_live: StrictBool = False

    @field_validator("input_usd_per_million", "output_usd_per_million")
    @classmethod
    def reject_non_positive_rates(cls, value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        if value <= 0 or not value.is_finite():
            raise ValueError("token price must be a positive finite number, or omitted")
        return value

    @property
    def api_key_status(self) -> str:
        return "present" if self.anthropic_api_key else "absent"

    @property
    def model_status(self) -> str:
        return self.model if self.model else "UNSET"

    @property
    def var_dir(self) -> Path:
        return self.repo_root / "var"

    @property
    def source_dir(self) -> Path:
        return self.data_dir / "source"


def detect_repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise ConfigError("Unable to locate repository root (missing pyproject.toml).")


def load_settings(
    *,
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path | None = None,
    repo_root: Path | None = None,
) -> Settings:
    root = repo_root or detect_repo_root()
    merged = _merge_environ(environ, dotenv_path if dotenv_path is not None else root / ".env")
    try:
        return Settings(
            repo_root=root,
            anthropic_api_key=_optional_secret(merged.get("ANTHROPIC_API_KEY")),
            model=_optional_text(merged.get("PRECEDENT_MODEL")),
            db_path=_resolve_path(root, merged.get("PRECEDENT_DB_PATH"), "var/precedent.sqlite3"),
            data_dir=_resolve_path(root, merged.get("PRECEDENT_DATA_DIR"), "data"),
            max_model_calls=_positive_int(
                "PRECEDENT_MAX_MODEL_CALLS", merged.get("PRECEDENT_MAX_MODEL_CALLS"), 10
            ),
            max_tool_calls=_positive_int(
                "PRECEDENT_MAX_TOOL_CALLS", merged.get("PRECEDENT_MAX_TOOL_CALLS"), 30
            ),
            max_case_seconds=_positive_int(
                "PRECEDENT_MAX_CASE_SECONDS", merged.get("PRECEDENT_MAX_CASE_SECONDS"), 120
            ),
            request_timeout_seconds=_positive_int(
                "PRECEDENT_REQUEST_TIMEOUT_SECONDS",
                merged.get("PRECEDENT_REQUEST_TIMEOUT_SECONDS"),
                30,
            ),
            max_output_tokens=_positive_int(
                "PRECEDENT_MAX_OUTPUT_TOKENS", merged.get("PRECEDENT_MAX_OUTPUT_TOKENS"), 1500
            ),
            input_usd_per_million=_optional_rate(
                "PRECEDENT_INPUT_USD_PER_MILLION", merged.get("PRECEDENT_INPUT_USD_PER_MILLION")
            ),
            output_usd_per_million=_optional_rate(
                "PRECEDENT_OUTPUT_USD_PER_MILLION", merged.get("PRECEDENT_OUTPUT_USD_PER_MILLION")
            ),
            enable_live=_strict_bool(
                "PRECEDENT_ENABLE_LIVE", merged.get("PRECEDENT_ENABLE_LIVE"), False
            ),
        )
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(str(exc)) from exc


def live_readiness_code(settings: Settings) -> str | None:
    """Return a local reason code if a live call must not be attempted."""
    if not settings.enable_live:
        return "LIVE_DISABLED"
    if not settings.anthropic_api_key:
        return "MISSING_CREDENTIALS"
    if not settings.model:
        return "MODEL_UNAVAILABLE"
    return None


def missing_live_variable_names(settings: Settings) -> tuple[str, ...]:
    """Return every live-setup variable that is missing or disabled.

    Names only; never values. ``live_readiness_code`` still reports the first
    blocking reason for command gating.
    """
    missing: list[str] = []
    if not settings.enable_live:
        missing.append("PRECEDENT_ENABLE_LIVE")
    if not settings.anthropic_api_key:
        missing.append("ANTHROPIC_API_KEY")
    if not settings.model:
        missing.append("PRECEDENT_MODEL")
    return tuple(missing)


def _merge_environ(environ: Mapping[str, str] | None, dotenv_path: Path) -> dict[str, str]:
    merged: dict[str, str] = {}
    if dotenv_path.is_file():
        for key, value in dotenv_values(dotenv_path).items():
            if key and value is not None:
                merged[key] = value
    source = os.environ if environ is None else environ
    for key, value in source.items():
        merged[key] = value
    return merged


def _optional_secret(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = raw.strip()
    return text or None


def _optional_text(raw: str | None) -> str | None:
    return _optional_secret(raw)


def _resolve_path(repo_root: Path, raw: str | None, default: str) -> Path:
    text = default if raw is None or not raw.strip() else raw.strip()
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve()


def _positive_int(name: str, raw: str | None, default: int) -> int:
    if raw is None or not raw.strip():
        return default
    text = raw.strip()
    if text.startswith("-") and text[1:].isdigit():
        raise ConfigError(f"{name} must be a positive integer, got {text!r}")
    if not text.isdigit():
        raise ConfigError(f"{name} must be a base-10 positive integer, got {text!r}")
    value = int(text)
    if value <= 0:
        raise ConfigError(f"{name} must be a positive integer, got {text!r}")
    return value


def _optional_rate(name: str, raw: str | None) -> Decimal | None:
    if raw is None or not raw.strip():
        return None
    text = raw.strip()
    if not _RATE_PATTERN.fullmatch(text):
        raise ConfigError(
            f"{name} must be a positive base-10 decimal without scientific notation, got {text!r}"
        )
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ConfigError(f"{name} is not a valid decimal, got {text!r}") from exc
    if value <= 0 or not value.is_finite():
        raise ConfigError(f"{name} must be a positive finite rate, got {text!r}")
    return value


def _strict_bool(name: str, raw: str | None, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    text = raw.strip().lower()
    if text == "true":
        return True
    if text == "false":
        return False
    raise ConfigError(f"{name} must be 'true' or 'false', got {raw.strip()!r}")


def public_settings_view(settings: Settings) -> dict[str, Any]:
    return {
        "repo_root": str(settings.repo_root),
        "db_path": str(settings.db_path),
        "data_dir": str(settings.data_dir),
        "max_model_calls": settings.max_model_calls,
        "max_tool_calls": settings.max_tool_calls,
        "max_case_seconds": settings.max_case_seconds,
        "request_timeout_seconds": settings.request_timeout_seconds,
        "max_output_tokens": settings.max_output_tokens,
        "input_usd_per_million": (
            "unknown"
            if settings.input_usd_per_million is None
            else str(settings.input_usd_per_million)
        ),
        "output_usd_per_million": (
            "unknown"
            if settings.output_usd_per_million is None
            else str(settings.output_usd_per_million)
        ),
        "enable_live": settings.enable_live,
        "model": settings.model_status,
        "api_key": settings.api_key_status,
    }


__all__ = [
    "ConfigError",
    "Settings",
    "detect_repo_root",
    "live_readiness_code",
    "load_settings",
    "missing_live_variable_names",
    "public_settings_view",
]
