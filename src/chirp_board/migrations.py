"""Packaged Board migration discovery."""

from pathlib import Path


def migration_directory() -> Path:
    """Return the installed, package-owned Board migration directory."""
    return Path(__file__).with_name("migrations")
