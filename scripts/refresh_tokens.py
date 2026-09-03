"""Per-platform credential refresh, so tokens don't expire mid-cadence.

Run as a scheduled task (see scripts/register_tokens.ps1) or by hand:

    python scripts/refresh_tokens.py instagram
    python scripts/refresh_tokens.py tiktok
    python scripts/refresh_tokens.py youtube
    python scripts/refresh_tokens.py all

Only Instagram can genuinely *refresh* here (Meta re-issues a long-lived token
from the current one). TikTok's 24-hour token and YouTube's refresh token
cannot be renewed from an access token alone - those two report what needs a
human's attention instead of pretending. Nothing ever writes a token to disk;
the new token goes straight back into the OS keychain via credentials.vault
(env-var-only fallback can't persist a refresh, so a keyring is required for a
refreshed token to survive - the script flags that case).

Exit codes: 0 = refreshed/checked OK, 2 = nothing to do (no creds configured),
3 = refresh attempted but failed. Never writes to a file.
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional

import requests

from credentials.vault import Vault

GRAPH_BASE = "https://graph.facebook.com/v19.0"


def _vault() -> Vault:
    return Vault()


def refresh_instagram() -> int:
    vault = _vault()
    try:
        token = vault.get("instagram_access_token")
    except Exception:
        print("instagram: no access token in the vault - nothing to refresh (set instagram_access_token first)")
        return 2

    resp = requests.get(
        f"{GRAPH_BASE}/oauth/access_token",
        params={
            "grant_type": "ig_exchange_token",
            "access_token": token,
        },
        timeout=30,
    )
    if not resp.ok:
        print(f"instagram: refresh failed: {resp.status_code} {resp.text[:300]}")
        return 3

    data = resp.json()
    new_token = data.get("access_token")
    if not new_token:
        print(f"instagram: refresh returned no access_token: {data}")
        return 3

    # Optional expiry probe: the response includes expires_in when available.
    expires_in = data.get("expires_in")
    try:
        vault.set("instagram_access_token", new_token)
    except Exception as exc:
        # vault.set deliberately raises if keyring has no backend - it will not
        # fall back to a file. That's correct for security; surface it loudly so
        # the scheduled job isn't silently keeping an empty promise.
        print(f"instagram: refreshed but COULD NOT persist to keyring: {exc}")
        return 3

    window = int(expires_in) // 86400 if expires_in else "unknown"
    print(f"instagram: access token refreshed and stored (expires ~{window} days)")

    # The publisher path also caches ig_user_id; confirm it's present.
    try:
        ig_user_id = vault.get("instagram_ig_user_id")
        print(f"instagram: ig_user_id present ({ig_user_id[:6]}...)" if ig_user_id else "instagram: ig_user_id MISSING")
    except Exception:
        print("instagram: ig_user_id missing - uploads will fail auth")
        return 3
    return 0


def refresh_tiktok() -> int:
    print(
        "tiktok: cannot auto-refresh from an access token alone. TikTok's 24h "
        "token needs an OAuth authorization-code refresh using the app's "
        "client_id/client_secret (an interactive or app-server flow). Store a "
        "fresh access_token in the vault before it expires, or verify the "
        "backend implements the full OAuth refresh. Nothing auto-refreshable here."
    )
    return 2


def refresh_youtube() -> int:
    print(
        "youtube: no long-lived-token refresh endpoint - google-auth refreshes "
        "on the first API call automatically from youtube_refresh_token / "
        "client_id / client_secret. One-time HUMAN check required: confirm the "
        "Google Cloud OAuth consent screen is Production/Published (Testing "
        "tokens expire in 7 days). See README 'YouTube Data API v3 upload'."
    )
    return 2


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="refresh_tokens", description="Refresh per-platform credentials via the vault.")
    parser.add_argument("platform", choices=["instagram", "tiktok", "youtube", "all"])
    args = parser.parse_args(argv)

    handlers = {
        "instagram": refresh_instagram,
        "tiktok": refresh_tiktok,
        "youtube": refresh_youtube,
    }
    if args.platform == "all":
        codes = []
        for name, fn in handlers.items():
            print(f"\n== {name} ==")
            codes.append(fn())
        non_ok = [c for c in codes if c not in (0, 2)]
        return 3 if non_ok else 0
    return handlers[args.platform]()


if __name__ == "__main__":
    sys.exit(main())
