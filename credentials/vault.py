"""Credential access. Nothing here reads a plaintext file. The vault talks to
the OS keychain via `keyring` first; if `keyring` isn't installed or has no
usable backend (common on headless servers/containers), it falls back to an
environment variable - still not a file on disk, though it's on the caller
to keep that environment itself secured (e.g. injected by a secrets manager
at deploy time, not committed anywhere).
"""
from __future__ import annotations

import os

SERVICE_NAME = "faceless-pipeline"


class CredentialNotFound(KeyError):
    pass


class Vault:
    def get(self, key: str) -> str:
        value = self._try_keyring(key)
        if value:
            return value

        env_key = _to_env_var(key)
        value = os.environ.get(env_key)
        if value:
            return value

        raise CredentialNotFound(
            f"no credential found for {key!r} - set it with "
            f"`keyring set {SERVICE_NAME} {key}` or export {env_key}"
        )

    def set(self, key: str, value: str) -> None:
        """Writes to the OS keychain. Raises if keyring has no usable backend -
        deliberately does NOT fall back to writing a file."""
        import keyring  # type: ignore[import-not-found]  # optional dependency: local import so vault works without it
        keyring.set_password(SERVICE_NAME, key, value)

    @staticmethod
    def _try_keyring(key: str) -> str | None:
        try:
            import keyring  # type: ignore[import-not-found]  # optional dependency, guarded by the except below
            return keyring.get_password(SERVICE_NAME, key)
        except ImportError:
            return None
        except Exception:
            # keyring is installed but has no usable backend (headless containers
            # commonly hit this) - fall through to the env var path rather than crash
            return None


def _to_env_var(key: str) -> str:
    return key.upper().replace("-", "_")


def credentials_provider(platform: str) -> dict:
    """Bridges the vault's flat key/value store to what each Publisher's
    constructor expects. This is the one function that knows the *shape*
    each platform's credentials come in; the vault itself stays generic."""
    vault = Vault()
    if platform == "youtube":
        from google.oauth2.credentials import Credentials  # type: ignore[import-untyped]  # google-auth ships no stubs

        # YouTube's credentials are a google-auth Credentials object, not a
        # dict, but the caller only passes them opaquely to build_client(); the
        # other platforms return a plain dict.
        return Credentials(  # type: ignore[return-value]
            token=None,  # left blank on purpose - refreshed automatically on first API call
            refresh_token=vault.get("youtube_refresh_token"),
            client_id=vault.get("youtube_client_id"),
            client_secret=vault.get("youtube_client_secret"),
            token_uri="https://oauth2.googleapis.com/token",
        )
    if platform == "instagram":
        return {
            "ig_user_id": vault.get("instagram_ig_user_id"),
            "access_token": vault.get("instagram_access_token"),
        }
    if platform == "tiktok":
        return {"access_token": vault.get("tiktok_access_token")}
    raise ValueError(f"unknown platform {platform!r}")
