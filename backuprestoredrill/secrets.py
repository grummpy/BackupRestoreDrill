"""Hostinger token from the environment or the OS keychain. Never written to config."""

from __future__ import annotations

import os

SERVICE = "backuprestoredrill"
ACCOUNT = "hostinger_api_token"
ENV_VAR = "HOSTINGER_API_TOKEN"


def get_token() -> str | None:
    """Prefer HOSTINGER_API_TOKEN, then the OS keychain."""
    from_env = os.environ.get(ENV_VAR, "").strip()
    if from_env:
        return from_env
    try:
        import keyring
    except Exception:
        return None
    try:
        stored = keyring.get_password(SERVICE, ACCOUNT)
    except Exception:
        return None
    if stored and stored.strip():
        return stored.strip()
    return None


def set_token(token: str) -> None:
    cleaned = token.strip()
    if not cleaned:
        raise ValueError("The token is empty.")
    import keyring

    keyring.set_password(SERVICE, ACCOUNT, cleaned)


def delete_token() -> None:
    try:
        import keyring

        keyring.delete_password(SERVICE, ACCOUNT)
    except Exception:
        return
