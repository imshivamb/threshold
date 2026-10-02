"""Holds the Ring bearer token. Playground tokens last ~30 minutes and are pasted at runtime."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

PLAYGROUND_TOKEN_TTL = timedelta(minutes=30)


@dataclass
class TokenStore:
    mode: str  # playground / emulator
    token: str | None = None
    set_at: datetime | None = None
    expired: bool = False

    def set(self, token: str) -> None:
        self.token = token.strip()
        self.set_at = datetime.now(tz=UTC)
        self.expired = False

    def current(self) -> str | None:
        return None if self.expired else self.token

    def mark_expired(self) -> None:
        # The emulator token never expires; a 401 there is a real misconfiguration.
        if self.mode == "playground":
            self.expired = True

    def status(self) -> dict:
        expires_at = self.set_at + PLAYGROUND_TOKEN_TTL if self.set_at and self.mode == "playground" else None
        return {
            "mode": self.mode,
            "has_token": bool(self.token),
            "expired": self.expired,
            "set_at": self.set_at,
            "expected_expiry": expires_at,
        }
