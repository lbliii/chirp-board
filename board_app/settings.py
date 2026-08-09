"""Application-owned settings that deliberately do not extend AppConfig."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
STATIC = ROOT / "static"


@dataclass(frozen=True, slots=True)
class BoardSettings:
    """Required runtime secrets and connection strings for Chirp Board."""

    database_url: str = field(repr=False)
    setup_token: str = field(repr=False)
    secret_key: str = field(repr=False)

    def __post_init__(self) -> None:
        missing = [
            name
            for name, value in (
                ("DATABASE_URL", self.database_url),
                ("WORKSPACE_SETUP_TOKEN", self.setup_token),
                ("CHIRP_SECRET_KEY", self.secret_key),
            )
            if not value
        ]
        if missing:
            raise RuntimeError("Chirp Board requires: " + ", ".join(missing))
        if len(self.setup_token) < 24:
            raise RuntimeError("WORKSPACE_SETUP_TOKEN must contain at least 24 characters")
        if len(self.secret_key) < 32:
            raise RuntimeError("CHIRP_SECRET_KEY must contain at least 32 characters")

    @classmethod
    def from_env(cls, *, database_url: str | None = None) -> BoardSettings:
        """Load settings from the process environment."""

        return cls(
            database_url=database_url
            or os.environ.get("DATABASE_URL", "")
            or f"sqlite:///{ROOT / 'board.db'}",
            setup_token=os.environ.get("WORKSPACE_SETUP_TOKEN", ""),
            secret_key=os.environ.get("CHIRP_SECRET_KEY", ""),
        )

    @classmethod
    def for_tests(cls, database_url: str) -> BoardSettings:
        """Deterministic local settings used by acceptance fixtures."""

        return cls(
            database_url=database_url,
            setup_token="local-board-setup-token-24chars",
            secret_key="local-board-secret-key-with-32-bytes!!",
        )
