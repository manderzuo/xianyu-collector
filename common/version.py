"""Build version lookup shared by all runtime services."""
from __future__ import annotations

import os
from pathlib import Path


def app_version(default: str = "0.0.0") -> str:
    """Return the release version embedded in the current runtime image.

    ``APP_VERSION`` is useful for explicitly supplied development builds. In
    containers the release file at ``/app/VERSION.txt`` is authoritative; the
    repository-relative fallback keeps local service launches consistent too.
    """
    configured = os.environ.get("APP_VERSION", "").strip()
    if configured:
        return configured
    candidates = [
        Path("/app/VERSION.txt"),
        Path(__file__).resolve().parents[1] / "VERSION.txt",
    ]
    for candidate in candidates:
        try:
            value = candidate.read_text(encoding="utf-8-sig").splitlines()[0].strip()
        except (OSError, UnicodeError, IndexError):
            continue
        if value:
            return value
    return default


__all__ = ["app_version"]
