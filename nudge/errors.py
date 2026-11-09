"""Exceptions shared across layers.

Kept in a leaf module so the engine can catch an auth failure without importing
the Google client stack, which costs seconds of startup time.
"""
from __future__ import annotations


class AuthError(Exception):
    """Credentials are missing or unrecoverable. Worth a DM, not a crash."""
