"""Runtime settings and secret loading.

Secrets come only from environment variables or the local ``.env`` file and are
held as ``SecretStr`` so they never appear in ``repr()``/logs/API responses.
Non-secret configuration lives in ``config/*.yaml``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

SECRET_FIELDS: tuple[str, ...] = (
    "github_token",
    "reddit_client_id",
    "reddit_client_secret",
    "anthropic_api_key",
    "x_api_key",
    "x_api_secret",
    "x_access_token",
    "x_access_token_secret",
    "x_bearer_token",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    github_token: SecretStr | None = None
    reddit_client_id: SecretStr | None = None
    reddit_client_secret: SecretStr | None = None
    reddit_user_agent: str | None = None
    solana_rpc_url: str | None = None
    anthropic_api_key: SecretStr | None = None
    x_api_key: SecretStr | None = None
    x_api_secret: SecretStr | None = None
    x_access_token: SecretStr | None = None
    x_access_token_secret: SecretStr | None = None
    x_bearer_token: SecretStr | None = None

    # Second, independent switch for live X posting (Stage 10). Only the exact value
    # "enabled" counts; config/publishing.yaml must also say mode: "live".
    liquidmexx_live_posting: str | None = None

    liquidmexx_data_dir: str | None = Field(default=None)
    liquidmexx_config_dir: str | None = Field(default=None)

    @property
    def data_dir(self) -> Path:
        return Path(self.liquidmexx_data_dir) if self.liquidmexx_data_dir else PROJECT_ROOT / "data"

    @property
    def config_dir(self) -> Path:
        if self.liquidmexx_config_dir:
            return Path(self.liquidmexx_config_dir)
        return PROJECT_ROOT / "config"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "liquidmexx.db"

    @property
    def live_posting_opt_in(self) -> bool:
        return (self.liquidmexx_live_posting or "").strip() == "enabled"

    def secret_values(self) -> list[str]:
        """All configured secret values (for log redaction). Never print this."""
        values: list[str] = []
        for name in SECRET_FIELDS:
            secret = getattr(self, name)
            if isinstance(secret, SecretStr):
                raw = secret.get_secret_value()
                if raw:
                    values.append(raw)
        return values

    def credential_status(self) -> dict[str, bool]:
        """Which credentials are present -- booleans only, never values."""
        return {
            name: bool(getattr(self, name) and getattr(self, name).get_secret_value())
            for name in SECRET_FIELDS
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
