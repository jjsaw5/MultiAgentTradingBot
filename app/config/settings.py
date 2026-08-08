"""Environment-driven runtime settings.

Credentials live here and nowhere else. Nothing in this module may be logged
directly -- use :meth:`Settings.safe_dict` when emitting settings to logs.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from typing import Any

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProviderBackend(str, Enum):
    MOCK = "mock"
    REST = "rest"
    MCP = "mcp"


class LLMBackend(str, Enum):
    SCRIPTED = "scripted"
    ANTHROPIC = "anthropic"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    app_env: str = "development"
    log_level: str = "INFO"
    log_format: str = "console"

    database_url: str = "sqlite:///./data/matb.db"

    # --- Safety -----------------------------------------------------------
    enable_order_execution: bool = False

    # --- LLM --------------------------------------------------------------
    llm_backend: LLMBackend = LLMBackend.SCRIPTED
    anthropic_api_key: SecretStr | None = None
    llm_model: str = "claude-opus-5"
    llm_max_tokens: int = 16000
    llm_timeout_seconds: int = 300

    # --- Providers --------------------------------------------------------
    fmp_backend: ProviderBackend = ProviderBackend.MOCK
    fmp_api_key: SecretStr | None = None
    fmp_base_url: str = "https://financialmodelingprep.com"

    unusual_whales_backend: ProviderBackend = ProviderBackend.MOCK
    unusual_whales_api_key: SecretStr | None = None
    unusual_whales_base_url: str = "https://api.unusualwhales.com"

    robinhood_backend: ProviderBackend = ProviderBackend.MOCK
    robinhood_mcp_endpoint: str | None = None

    news_backend: ProviderBackend = ProviderBackend.MOCK
    news_api_key: SecretStr | None = None

    provider_timeout_seconds: int = 15
    provider_max_retries: int = 2

    mock_seed: int = 20240101

    methodology_config_path: str = "config/methodology.yaml"

    @field_validator("enable_order_execution")
    @classmethod
    def _execution_must_stay_disabled(cls, v: bool) -> bool:
        # The MVP contains no execution code path. If someone flips this flag
        # expecting orders to be placed, fail loudly rather than silently.
        if v:
            raise ValueError(
                "ENABLE_ORDER_EXECUTION=true is not supported: this system is "
                "research-only and implements no order submission."
            )
        return v

    def safe_dict(self) -> dict[str, Any]:
        """Settings with every secret redacted -- the only log-safe view."""
        out: dict[str, Any] = {}
        for name, value in self.model_dump().items():
            if isinstance(value, SecretStr) or name.endswith(("_api_key", "_token", "_secret")):
                out[name] = "***set***" if value else "***unset***"
            else:
                out[name] = value
        return out

    def secret(self, name: str) -> str | None:
        value = getattr(self, name, None)
        if isinstance(value, SecretStr):
            return value.get_secret_value() or None
        return value or None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()


__all__ = [
    "LLMBackend",
    "ProviderBackend",
    "Settings",
    "get_settings",
    "reset_settings_cache",
]
