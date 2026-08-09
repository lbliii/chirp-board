"""Local and Railway entry point for Chirp Board."""

from __future__ import annotations

import os

from board_app import create_board_runtime
from board_app.settings import BoardSettings

_settings = None
if os.environ.get("CHIRP_SECRET_KEY") and os.environ.get("WORKSPACE_SETUP_TOKEN"):
    _settings = BoardSettings.from_env()
elif os.environ.get("CHIRP_ENV", "development") == "development":
    _settings = BoardSettings(
        database_url=os.environ.get("DATABASE_URL", "sqlite:///board.db"),
        setup_token=os.environ.get("WORKSPACE_SETUP_TOKEN", "local-board-setup-token-24chars"),
        secret_key=os.environ.get("CHIRP_SECRET_KEY", "local-board-secret-key-with-32-bytes!!"),
    )
else:
    _settings = BoardSettings.from_env()

runtime = create_board_runtime(_settings)
app = runtime.app

if __name__ == "__main__":
    app.run()
