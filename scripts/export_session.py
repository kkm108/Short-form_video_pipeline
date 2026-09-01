"""One-time helper: opens a real, visible browser, lets you log into your
chosen media-generation tool by hand (including any 2FA/CAPTCHA), then saves
the resulting session so browser_use_adapter.py can reuse it without logging
in again on every run.

This is a thin Playwright script, not browser-use itself - exporting
storage_state is a one-time interactive step a human has to do; browser-use
only needs to *consume* the resulting file later. Keeping these separate
means this script's only dependency is `playwright`, not the heavier
`browser-use` package.

Usage:
    pip install playwright
    playwright install chromium
    python3 scripts/export_session.py https://your-tool.example.com/login ./profiles/generation_tool.json
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path


async def _export(start_url: str, output_path: str) -> None:
    from playwright.async_api import async_playwright

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(start_url)

        print("\nA browser window just opened.")
        print("Log in by hand - complete any 2FA or CAPTCHA now, this only happens once.")
        input("Once you're logged in and can see the tool's normal (post-login) page, press Enter here...\n")

        await context.storage_state(path=output_path)
        await browser.close()

        print(f"Saved session to {output_path}")
        print(
            "This file is a live, logged-in credential - treat it like an API key. "
            "It's already covered by .gitignore (profiles/*.json); never commit it or share it."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("start_url", help="Login page (or homepage) of the tool you're exporting a session for")
    parser.add_argument("output_path", help="Where to save the session, e.g. ./profiles/generation_tool.json")
    args = parser.parse_args()
    asyncio.run(_export(args.start_url, args.output_path))


if __name__ == "__main__":
    main()
