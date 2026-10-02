from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

PLAYGROUND_BASE_URL = "https://api.amazonvision.com"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://threshold:threshold@localhost:5432/threshold"

    ring_mode: Literal["playground", "emulator"] = "emulator"
    ring_playground_url: str = PLAYGROUND_BASE_URL
    ring_emulator_url: str = "http://localhost:8787"
    # Overrides both URLs above; handy for pointing playground mode at a local emulator.
    ring_base_url: str | None = None
    # The emulator accepts any non-empty bearer token.
    ring_emulator_token: str = "sandbox"
    ring_playground_token: str | None = None
    ring_webhook_signing_key: str = "dev-signing-key-change-me"

    # Where the emulator should deliver webhooks (as seen from the emulator's network).
    public_webhook_url: str = "http://localhost:8000/webhooks/ring"
    register_emulator_webhook: bool = True

    poll_interval_seconds: int = 15
    # Run the history poller in emulator mode too (playground mode always polls).
    poll_in_emulator_mode: bool = False

    # Silence Watch: how often it runs, and when a camera's heartbeat counts as stale.
    watch_interval_seconds: int = 60
    device_stale_hours: float = 2.0

    household_name: str = "Demo household"
    household_timezone: str = "Asia/Kolkata"

    @property
    def ring_url(self) -> str:
        if self.ring_base_url:
            return self.ring_base_url.rstrip("/")
        url = self.ring_playground_url if self.ring_mode == "playground" else self.ring_emulator_url
        return url.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
