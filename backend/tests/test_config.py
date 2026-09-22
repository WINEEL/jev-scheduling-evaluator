import pytest

from app.config import (
    ENV_FILE,
    REPO_ROOT,
    Settings,
    load_settings,
    redact_url,
)


def test_redact_url_removes_credentials():
    redacted = redact_url(
        "postgresql+psycopg://user:secretpw@db.example.com:5432/appdb?sslmode=require"
    )

    assert "secretpw" not in redacted
    assert "user" not in redacted
    assert "db.example.com" in redacted
    assert redacted.endswith("/appdb")


def test_redact_url_handles_garbage():
    assert redact_url("not a url") == "<database url with no host>"


def test_missing_database_url_raises_clear_error(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError) as excinfo:
        # env_file=None -> ignore any .env so the value is genuinely absent
        load_settings(env_file=None)

    message = str(excinfo.value)
    assert "DATABASE_URL" in message
    assert "secret" not in message.lower()
    assert "test_password" not in message


def test_settings_load_from_a_dotenv_file(tmp_path, monkeypatch):
    """The .env-loading mechanism works with a throwaway file (no real .env)."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=postgresql+psycopg://u:p@example.invalid:5432/d\n"
        "APP_ENV=staging\n"
    )

    settings = load_settings(env_file=env_file)

    assert settings.database_url == "postgresql+psycopg://u:p@example.invalid:5432/d"
    assert settings.app_env == "staging"


def test_environment_overrides_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://env:env@env.invalid:5432/db"
    )
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=postgresql+psycopg://file:file@file.invalid:5432/db\n"
    )

    settings = load_settings(env_file=env_file)

    assert settings.database_url == "postgresql+psycopg://env:env@env.invalid:5432/db"


def test_production_config_targets_repo_root_dotenv():
    """Production behavior is unchanged: DATABASE_URL required, repo-root .env used."""
    assert Settings.model_config["env_file"] == ENV_FILE
    assert ENV_FILE == REPO_ROOT / ".env"
    assert Settings.model_fields["database_url"].is_required()
