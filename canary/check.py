"""Runs on its own schedule (cron / CI job), separate from real pipeline
runs, to catch UI drift on the browser-automation step *before* it breaks a
live run. Point this at a disposable test account, never the real channel's
session - a canary that itself risks the production account defeats the
point.

Fill in EXPECTED_ELEMENTS for whichever generation tool
executors/browser_use_adapter.py is pointed at; there's nothing generic to
check until you've picked a specific tool and looked at its markup.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("pipeline.canary")

# (selector, human-readable description) - e.g.:
# EXPECTED_ELEMENTS = [("button[aria-label='Generate']", "generate button")]
EXPECTED_ELEMENTS: list[tuple[str, str]] = []


async def _check(session_profile: str, target_url: str) -> bool:
    from browser_use import Browser  # type: ignore[import-not-found]  # lazy import - same optional dependency as the adapter

    if not EXPECTED_ELEMENTS:
        logger.warning("canary has no EXPECTED_ELEMENTS configured - nothing to check yet")
        return True

    browser = Browser(storage_state=session_profile, headless=True)
    page = await browser.new_page()
    await page.goto(target_url)

    all_found = True
    for selector, description in EXPECTED_ELEMENTS:
        found = await page.locator(selector).count() > 0
        if not found:
            logger.error("canary: missing expected element %r (%s)", selector, description)
            all_found = False
    return all_found


def run_canary(session_profile: str, target_url: str) -> bool:
    """Returns False on drift. Wire the return value to whatever pages you -
    Slack, PagerDuty, a cron exit code your scheduler treats as a failure -
    and have that gate whether the next scheduled real run is allowed to
    start. A red canary should pause scheduled runs, not just log a line."""
    ok = asyncio.run(_check(session_profile, target_url))
    if not ok:
        logger.error("canary FAILED - target site's UI likely changed; pausing scheduled runs")
    return ok
