from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest

from precedent.cli import main
from precedent.config import ConfigError, live_readiness_code, load_settings

CONFIG_ENV = (
    "ANTHROPIC_API_KEY",
    "PRECEDENT_MODEL",
    "PRECEDENT_DB_PATH",
    "PRECEDENT_DATA_DIR",
    "PRECEDENT_MAX_MODEL_CALLS",
    "PRECEDENT_MAX_TOOL_CALLS",
    "PRECEDENT_MAX_CASE_SECONDS",
    "PRECEDENT_REQUEST_TIMEOUT_SECONDS",
    "PRECEDENT_MAX_OUTPUT_TOKENS",
    "PRECEDENT_INPUT_USD_PER_MILLION",
    "PRECEDENT_OUTPUT_USD_PER_MILLION",
    "PRECEDENT_ENABLE_LIVE",
)


@pytest.fixture
def clean_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in CONFIG_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def isolated_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_config_env: None) -> Path:
    def _load():
        return load_settings(
            environ=os.environ,
            dotenv_path=tmp_path / "missing.env",
            repo_root=tmp_path,
        )

    monkeypatch.setattr("precedent.cli.load_settings", _load)
    return tmp_path


def test_defaults_do_not_require_credentials(tmp_path: Path) -> None:
    settings = load_settings(
        environ={},
        dotenv_path=tmp_path / "missing.env",
        repo_root=tmp_path,
    )
    assert settings.anthropic_api_key is None
    assert settings.api_key_status == "absent"
    assert settings.model is None
    assert settings.enable_live is False
    assert settings.max_model_calls == 10
    assert settings.max_tool_calls == 30
    assert settings.max_case_seconds == 120
    assert settings.request_timeout_seconds == 30
    assert settings.max_output_tokens == 1500
    assert settings.input_usd_per_million is None
    assert settings.output_usd_per_million is None
    assert settings.db_path == (tmp_path / "var/precedent.sqlite3").resolve()
    assert settings.data_dir == (tmp_path / "data").resolve()


def test_environment_overrides_dotenv(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("PRECEDENT_MAX_MODEL_CALLS=8\nPRECEDENT_ENABLE_LIVE=true\n", encoding="utf-8")
    settings = load_settings(
        environ={"PRECEDENT_MAX_MODEL_CALLS": "12", "PRECEDENT_ENABLE_LIVE": "false"},
        dotenv_path=dotenv,
        repo_root=tmp_path,
    )
    assert settings.max_model_calls == 12
    assert settings.enable_live is False


def test_dotenv_used_when_environment_absent(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("PRECEDENT_MAX_TOOL_CALLS=7\n", encoding="utf-8")
    settings = load_settings(environ={}, dotenv_path=dotenv, repo_root=tmp_path)
    assert settings.max_tool_calls == 7


def test_absent_prices_are_unknown_not_zero(tmp_path: Path) -> None:
    settings = load_settings(environ={}, dotenv_path=tmp_path / "x.env", repo_root=tmp_path)
    assert settings.input_usd_per_million is None
    assert settings.output_usd_per_million is None


@pytest.mark.parametrize("raw", ["0", "-1", "1.5", "true", "10.0", "1e2", "+3"])
def test_invalid_limits_are_rejected(tmp_path: Path, raw: str) -> None:
    with pytest.raises(ConfigError, match="PRECEDENT_MAX_MODEL_CALLS"):
        load_settings(
            environ={"PRECEDENT_MAX_MODEL_CALLS": raw},
            dotenv_path=tmp_path / "x.env",
            repo_root=tmp_path,
        )


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "1e-3", "true"])
def test_nonsensical_rates_are_rejected(tmp_path: Path, raw: str) -> None:
    with pytest.raises(ConfigError, match="PRECEDENT_INPUT_USD_PER_MILLION"):
        load_settings(
            environ={"PRECEDENT_INPUT_USD_PER_MILLION": raw},
            dotenv_path=tmp_path / "x.env",
            repo_root=tmp_path,
        )


def test_positive_rate_is_kept_as_decimal(tmp_path: Path) -> None:
    settings = load_settings(
        environ={"PRECEDENT_INPUT_USD_PER_MILLION": "3.50"},
        dotenv_path=tmp_path / "x.env",
        repo_root=tmp_path,
    )
    assert settings.input_usd_per_million == Decimal("3.50")


def test_invalid_live_flag_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="PRECEDENT_ENABLE_LIVE"):
        load_settings(
            environ={"PRECEDENT_ENABLE_LIVE": "yes"},
            dotenv_path=tmp_path / "x.env",
            repo_root=tmp_path,
        )


def test_live_readiness_missing_credentials(tmp_path: Path) -> None:
    settings = load_settings(
        environ={"PRECEDENT_ENABLE_LIVE": "true", "PRECEDENT_MODEL": "claude-test"},
        dotenv_path=tmp_path / "x.env",
        repo_root=tmp_path,
    )
    assert live_readiness_code(settings) == "MISSING_CREDENTIALS"


def test_live_readiness_disabled_even_with_key(tmp_path: Path) -> None:
    settings = load_settings(
        environ={
            "PRECEDENT_ENABLE_LIVE": "false",
            "ANTHROPIC_API_KEY": "secret-test-key",
            "PRECEDENT_MODEL": "claude-test",
        },
        dotenv_path=tmp_path / "x.env",
        repo_root=tmp_path,
    )
    assert live_readiness_code(settings) == "LIVE_DISABLED"


def test_offline_preflight_passes_without_credentials(
    isolated_cli: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["preflight", "--offline"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Result: PASS" in out
    assert "NOT_READY" in out
    assert "api_key: absent" in out
    assert "streamlit" in out
    assert "secret" not in out.lower()
    assert (isolated_cli / "var").is_dir()


def test_offline_preflight_invalid_limit_exits_2(
    isolated_cli: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PRECEDENT_MAX_MODEL_CALLS", "0")
    code = main(["preflight", "--offline"])
    out = capsys.readouterr().out
    assert code == 2
    assert "INVALID_CONFIG" in out
    assert "PRECEDENT_MAX_MODEL_CALLS" in out


def test_live_preflight_disabled(isolated_cli: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["preflight", "--live"])
    out = capsys.readouterr().out
    assert code == 2
    assert "LIVE_DISABLED" in out
    assert "Result: FAIL" in out


def test_live_preflight_missing_credentials(
    isolated_cli: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PRECEDENT_ENABLE_LIVE", "true")
    monkeypatch.setenv("PRECEDENT_MODEL", "claude-test-model")
    code = main(["preflight", "--live"])
    out = capsys.readouterr().out
    assert code == 2
    assert "MISSING_CREDENTIALS" in out
    assert "ANTHROPIC_API_KEY" in out


def test_live_preflight_missing_model(
    isolated_cli: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PRECEDENT_ENABLE_LIVE", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-test-key")
    code = main(["preflight", "--live"])
    out = capsys.readouterr().out
    assert code == 2
    assert "MODEL_UNAVAILABLE" in out
    assert "secret-test-key" not in out


def test_live_preflight_success_path_does_not_print_key(
    isolated_cli: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PRECEDENT_ENABLE_LIVE", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-test-key")
    monkeypatch.setenv("PRECEDENT_MODEL", "claude-test-model")
    monkeypatch.setattr(
        "precedent.cli._perform_live_model_request",
        lambda settings: "claude-test-model",
    )
    code = main(["preflight", "--live"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Result: PASS" in out
    assert "verified" in out
    assert "claude-test-model" in out
    assert "secret-test-key" not in out


def test_live_preflight_provider_error_exits_1(
    isolated_cli: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from precedent.cli import LivePreflightError

    monkeypatch.setenv("PRECEDENT_ENABLE_LIVE", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-test-key")
    monkeypatch.setenv("PRECEDENT_MODEL", "claude-test-model")

    def _boom(settings):
        raise LivePreflightError(
            "PROVIDER_UNAVAILABLE", "Network or connection error reaching the provider."
        )

    monkeypatch.setattr("precedent.cli._perform_live_model_request", _boom)
    code = main(["preflight", "--live"])
    out = capsys.readouterr().out
    assert code == 1
    assert "PROVIDER_UNAVAILABLE" in out
    assert "secret-test-key" not in out
