"""Minimal outward alerting for unattended failures, so a 3am breakage doesn't
fail silently. This is deliberately small and dependency-free on the pipeline:
one function that posts a plain-text message to a Slack-compatible incoming
webhook and never raises to its caller (a failed alert must not also crash
whatever was already failing).

Webhook URL comes from ``PIPELINE_ALERT_WEBHOOK`` (env var or, better, the
vault under ``pipeline_alert_webhook`` via ``credentials.vault.Vault``), never
a committed value. With no URL configured this module no-ops, so the default
behaviour is unchanged until someone opts into alerting.

Only ``send_alert`` is public; everything else is factored out so it can be
unit-tested against a local mock server without real HTTP (see
tests/test_alerter.py).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import requests

logger = logging.getLogger("pipeline.alert")

_DEFAULT_TIMEOUT_S = 15


def send_alert(message: str, webhook_url: Optional[str] = None) -> bool:
    """Post ``message`` to the alert webhook. Returns True if the alert was
    dispatched (or there was no webhook to talk to), False if delivery failed.
    Never raises.

    With no webhook configured, logs a warning the first time and no-ops.
    """
    url = webhook_url or _resolve_webhook_url()
    if not url:
        logger.warning("alert not sent - no PIPELINE_ALERT_WEBHOOK configured (message: %s)", message[:120])
        return True  # nothing to do, not an error

    payload = _build_payload(message)
    try:
        resp = requests.post(url, json=payload, timeout=_DEFAULT_TIMEOUT_S)
    except requests.RequestException as exc:
        logger.error("alert delivery failed: %s", exc)
        return False

    if not resp.ok:
        logger.error("alert delivery rejected (%s): %s", resp.status_code, resp.text[:200])
        return False
    return True


def _resolve_webhook_url() -> Optional[str]:
    """Prefer the vault; fall back to the plain env var. Both keep the URL out
    of any committed file. A vault value travelling through a headless context
    typically lands in the env var, so checking the env var second is cheap."""
    try:
        from credentials.vault import Vault  # lazy import - keep alerting importable without the rest
        return Vault().get("pipeline_alert_webhook")
    except Exception:
        return os.environ.get("PIPELINE_ALERT_WEBHOOK") or None


def _build_payload(message: str) -> dict:
    """Slack-compatible incoming-webhook payload: plain ``text`` (no buttons -
    this is a simple alert, not the review-gate interactive prompt)."""
    return {"text": message}
