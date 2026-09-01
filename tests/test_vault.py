"""keyring isn't installed in most headless build/CI environments (this
sandbox included), so these tests exercise the fallback path that will
actually run there: environment variables, never a plaintext file.
"""
from __future__ import annotations

import os

from credentials.vault import CredentialNotFound, Vault, credentials_provider


def test_get_falls_back_to_env_var():
    os.environ["TIKTOK_ACCESS_TOKEN"] = "tok_abc123"
    try:
        assert Vault().get("tiktok_access_token") == "tok_abc123"
        print("PASS test_get_falls_back_to_env_var")
    finally:
        del os.environ["TIKTOK_ACCESS_TOKEN"]


def test_get_raises_clear_error_when_nothing_is_set():
    os.environ.pop("SOME_MISSING_CRED", None)
    try:
        Vault().get("some_missing_cred")
        assert False, "expected CredentialNotFound"
    except CredentialNotFound as exc:
        assert "SOME_MISSING_CRED" in str(exc)  # tells the operator exactly what to export
        print("PASS test_get_raises_clear_error_when_nothing_is_set")


def test_credentials_provider_shapes_instagram_creds():
    os.environ["INSTAGRAM_IG_USER_ID"] = "17800000000000000"
    os.environ["INSTAGRAM_ACCESS_TOKEN"] = "ig_tok_xyz"
    try:
        creds = credentials_provider("instagram")
        assert creds == {"ig_user_id": "17800000000000000", "access_token": "ig_tok_xyz"}
        print("PASS test_credentials_provider_shapes_instagram_creds")
    finally:
        del os.environ["INSTAGRAM_IG_USER_ID"]
        del os.environ["INSTAGRAM_ACCESS_TOKEN"]


def test_credentials_provider_builds_youtube_oauth_credentials():
    try:
        import google.oauth2.credentials  # noqa: F401
    except ImportError:
        print("SKIP test_credentials_provider_builds_youtube_oauth_credentials (google-auth not installed here)")
        return

    os.environ["YOUTUBE_REFRESH_TOKEN"] = "refresh_abc"
    os.environ["YOUTUBE_CLIENT_ID"] = "client_123"
    os.environ["YOUTUBE_CLIENT_SECRET"] = "secret_xyz"
    try:
        creds = credentials_provider("youtube")
        # google.oauth2.credentials.Credentials - checking the real object's
        # attributes, not just that *something* came back, since this is
        # exactly the kind of "looks fine, wrong shape" bug that only shows
        # up once googleapiclient tries to actually use it.
        assert creds.refresh_token == "refresh_abc"
        assert creds.client_id == "client_123"
        assert creds.client_secret == "secret_xyz"
        assert creds.token_uri == "https://oauth2.googleapis.com/token"
        print("PASS test_credentials_provider_builds_youtube_oauth_credentials")
    finally:
        del os.environ["YOUTUBE_REFRESH_TOKEN"]
        del os.environ["YOUTUBE_CLIENT_ID"]
        del os.environ["YOUTUBE_CLIENT_SECRET"]


def test_credentials_provider_rejects_unknown_platform():
    try:
        credentials_provider("myspace")
        assert False, "expected ValueError"
    except ValueError:
        print("PASS test_credentials_provider_rejects_unknown_platform")


if __name__ == "__main__":
    test_get_falls_back_to_env_var()
    test_get_raises_clear_error_when_nothing_is_set()
    test_credentials_provider_shapes_instagram_creds()
    test_credentials_provider_builds_youtube_oauth_credentials()
    test_credentials_provider_rejects_unknown_platform()
    print("\nall vault tests passed")
