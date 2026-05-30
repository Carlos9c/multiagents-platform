"""Utilities for capturing the current system version from git."""

from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)

_cached_version: str | None = None
_cache_initialized: bool = False  # separate flag so "unknown" result is also cached


def get_system_version() -> str:
    """Return the current git commit hash, appending '-dirty' if the working tree
    has uncommitted changes.  Returns 'unknown' if git is not available.

    The result is cached after the first call so that multiple ExecutionRun
    creations in the same process don't repeatedly shell out to git.
    """
    global _cached_version, _cache_initialized
    if _cache_initialized:
        return _cached_version  # type: ignore[return-value]
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if commit.returncode != 0:
            _cached_version = "unknown"
        else:
            commit_hash = commit.stdout.strip()
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            is_dirty = status.returncode == 0 and bool(status.stdout.strip())
            _cached_version = f"{commit_hash}-dirty" if is_dirty else commit_hash
    except Exception:
        logger.warning("git_utils: could not determine system version", exc_info=True)
        _cached_version = "unknown"
    finally:
        _cache_initialized = True
    return _cached_version  # type: ignore[return-value]


def reset_cache() -> None:
    """Reset the cached version (used in tests that need a fresh read)."""
    global _cached_version, _cache_initialized
    _cached_version = None
    _cache_initialized = False
