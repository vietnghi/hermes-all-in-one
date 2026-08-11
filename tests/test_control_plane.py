from pathlib import Path

import pytest
import yaml

from control_plane import auth
from control_plane.auth import create_admin_session, verify_admin_session
from control_plane.config import (
    _SUPPORTED_PROVIDER_SETUPS,
    apply_provider_setup,
    has_valid_channel_credentials,
    is_masked_secret,
    load_env_file,
    mask_secret,
    save_channel_values,
    should_autostart_gateway,
    write_env_updates,
)


class _FakeRequest:
    """Minimal stand-in for starlette Request in auth helpers."""

    def __init__(self, ip: str = "203.0.113.7"):
        self.headers: dict[str, str] = {}
        self.cookies: dict[str, str] = {}
        self.client = type("C", (), {"host": ip})()


@pytest.fixture
def no_admin_password(monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_PASSWORD", "")
    monkeypatch.setattr(auth, "ADMIN_ALLOW_INSECURE", False)


@pytest.fixture
def with_admin_password(monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_PASSWORD", "correct-horse-battery")
    monkeypatch.setattr(auth, "ADMIN_ALLOW_INSECURE", False)


def test_write_env_updates_round_trips_values(tmp_path: Path):
    env_path = tmp_path / ".env"

    write_env_updates(env_path, {"OPENROUTER_API_KEY": "sk-test", "TELEGRAM_BOT_TOKEN": "123:abc"})

    values = load_env_file(env_path)
    assert values["OPENROUTER_API_KEY"] == "sk-test"
    assert values["TELEGRAM_BOT_TOKEN"] == "123:abc"


def test_apply_provider_setup_writes_yaml_and_env(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / ".env"

    result = apply_provider_setup(
        config_path=config_path,
        env_path=env_path,
        provider="openrouter",
        model="anthropic/claude-sonnet-4.6",
        api_key="sk-or-test",
        base_url="",
    )

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    values = load_env_file(env_path)

    assert result["provider"] == "openrouter"
    assert config["model"]["provider"] == "openrouter"
    assert config["model"]["default"] == "anthropic/claude-sonnet-4.6"
    assert values["OPENROUTER_API_KEY"] == "sk-or-test"


def test_apply_provider_setup_requires_base_url_for_custom(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / ".env"

    with pytest.raises(ValueError, match="base_url"):
        apply_provider_setup(
            config_path=config_path,
            env_path=env_path,
            provider="custom",
            model="gpt-4o-mini",
            api_key="sk-test",
            base_url="",
        )


def test_supported_provider_catalog_exposes_api_key_happy_path():
    assert "openrouter" in _SUPPORTED_PROVIDER_SETUPS
    assert _SUPPORTED_PROVIDER_SETUPS["openrouter"]["env_var"] == "OPENROUTER_API_KEY"
    assert _SUPPORTED_PROVIDER_SETUPS["custom"]["requires_base_url"] is True


def test_has_valid_channel_credentials_prefers_real_channel_tokens():
    assert has_valid_channel_credentials({"TELEGRAM_BOT_TOKEN": "123:abc"}) is True
    assert has_valid_channel_credentials({"DISCORD_BOT_TOKEN": "discord-token"}) is True
    assert has_valid_channel_credentials({"WHATSAPP_ENABLED": "1"}) is True
    assert has_valid_channel_credentials({"OPENROUTER_API_KEY": "sk-only"}) is False


def test_should_autostart_gateway_requires_provider_and_channel(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / ".env"

    config_path.write_text(yaml.safe_dump({"model": {"provider": "openrouter", "default": "anthropic/claude-sonnet-4.6"}}), encoding="utf-8")
    write_env_updates(env_path, {"OPENROUTER_API_KEY": "sk-or-test"})
    assert should_autostart_gateway(config_path=config_path, env_path=env_path) is False

    write_env_updates(env_path, {"TELEGRAM_BOT_TOKEN": "123:abc"})
    assert should_autostart_gateway(config_path=config_path, env_path=env_path) is True


def test_admin_session_round_trip():
    cookie = create_admin_session()
    assert verify_admin_session(cookie) is True


def test_admin_auth_fails_closed_without_password(no_admin_password):
    """No password must lock /admin, never open it."""
    assert auth.admin_auth_configured() is False
    assert auth.admin_auth_bypassed() is False
    assert auth.is_admin_authenticated(_FakeRequest()) is False
    assert auth.verify_admin_password("") is False
    assert auth.verify_admin_password("anything") is False


def test_admin_auth_rejects_too_short_password(monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_PASSWORD", "short")
    monkeypatch.setattr(auth, "ADMIN_ALLOW_INSECURE", False)
    assert auth.admin_password_state() == "too_short"
    assert auth.is_admin_authenticated(_FakeRequest()) is False
    assert auth.verify_admin_password("short") is False


def test_insecure_override_cannot_disable_a_configured_password(monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_PASSWORD", "correct-horse-battery")
    monkeypatch.setattr(auth, "ADMIN_ALLOW_INSECURE", True)
    assert auth.admin_auth_bypassed() is False
    assert auth.is_admin_authenticated(_FakeRequest()) is False


def test_insecure_override_opens_admin_only_without_password(monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_PASSWORD", "")
    monkeypatch.setattr(auth, "ADMIN_ALLOW_INSECURE", True)
    assert auth.admin_auth_bypassed() is True
    assert auth.is_admin_authenticated(_FakeRequest()) is True


def test_login_throttle_trips_after_max_attempts(monkeypatch, with_admin_password):
    monkeypatch.setattr(auth, "ADMIN_LOGIN_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(auth, "_LOGIN_FAILURES", {})
    request = _FakeRequest("198.51.100.4")

    for _ in range(3):
        assert auth.login_throttled(request) is False
        auth.record_login_failure(request)

    assert auth.login_throttled(request) is True
    assert auth.login_throttled(_FakeRequest("198.51.100.5")) is False

    auth.clear_login_failures(request)
    assert auth.login_throttled(request) is False


def test_mask_secret_hides_everything_but_the_tail():
    masked = mask_secret("123456:ABCdefGhIjKlMnOpQrStUv")
    assert masked == "••••StUv"
    assert "123456" not in masked
    assert is_masked_secret(masked) is True
    assert is_masked_secret("real-token") is False
    assert mask_secret("") == ""


def test_save_channel_values_ignores_echoed_masks(tmp_path: Path):
    env_path = tmp_path / ".env"
    write_env_updates(env_path, {"TELEGRAM_BOT_TOKEN": "123:realtoken"})

    save_channel_values(env_path, {"TELEGRAM_BOT_TOKEN": mask_secret("123:realtoken")})

    assert load_env_file(env_path)["TELEGRAM_BOT_TOKEN"] == "123:realtoken"


def test_apply_provider_setup_rejects_masked_api_key(tmp_path: Path):
    with pytest.raises(ValueError, match="masked placeholder"):
        apply_provider_setup(
            config_path=tmp_path / "config.yaml",
            env_path=tmp_path / ".env",
            provider="openrouter",
            model="anthropic/claude-sonnet-4.6",
            api_key=mask_secret("sk-or-realkey-value"),
            base_url="",
        )


def test_request_is_secure_reads_forwarded_proto(monkeypatch):
    monkeypatch.setattr(auth, "TRUST_PROXY_HEADERS", True)
    request = _FakeRequest()
    request.url = type("U", (), {"scheme": "http"})()

    assert auth.request_is_secure(request) is False
    request.headers["x-forwarded-proto"] = "https, http"
    assert auth.request_is_secure(request) is True

    monkeypatch.setattr(auth, "TRUST_PROXY_HEADERS", False)
    assert auth.request_is_secure(request) is False
