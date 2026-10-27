"""One headful Chrome on a persistent profile, launched so it behaves like your own browser.

Three choices here are deliberate and must not be "tidied up":

1. `ignore_default_args=["--enable-automation"]` and `--disable-blink-features=AutomationControlled`.
   Playwright, like chromedp, is a *testing* driver: its defaults optimise for "clearly identifiable
   as a test harness", which is the opposite of what a browser acting on your own logged-in session
   needs. `--enable-automation` sets `navigator.webdriver` and shows the "controlled by automated
   test software" infobar; the Blink feature sets the property even without the flag. Straight from
   Learned the hard way in an earlier project, where exactly this
   went unnoticed for months. `tests/test_browser_flags.py` asserts `navigator.webdriver` is unset,
   and that test fails if you remove either line.

2. `channel="chrome"` - your installed Chrome, not Playwright's bundled Chromium, for the same
   reason: fewer differences from the browser you normally use.

3. A **dedicated** profile directory under `data/profiles/`, NOT your real Chrome profile. Two
   reasons: `launch_persistent_context` takes an exclusive lock on the profile, so pointing it at
   your real one would fight your own open Chrome; and this project never needs to read your
   browsing data. You log in once inside this profile and it persists.

What we get for free from Playwright, and what an earlier project had to hand-build in chromedp:
`locator.fill()` goes through CDP `Input.insertText` (a trusted `beforeinput`/`input`, which is the
only thing a Lexical/Draft-class editor such as Facebook's composer accepts), `locator.click()`
dispatches real `Input.dispatchMouseEvent`s, and every action auto-waits for the element to be
actionable - which is the whole of `browser-contenteditable-insert-text-and-settle.md`.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from playwright.async_api import BrowserContext, async_playwright

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--window-size=1366,960",
]
DROP_DEFAULT_ARGS = ["--enable-automation"]


@asynccontextmanager
async def open_context(profile_dir: Path, *, headless: bool = False,
                       channel: str | None = "chrome") -> AsyncIterator[BrowserContext]:
    """Open (and always close) a persistent context on `profile_dir`.

    Headful by default. A publisher that hides the browser is a publisher you cannot watch fail,
    and a headless Chrome is more likely to be challenged on the sites this drives.
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(profile_dir),
            channel=channel,
            headless=headless,
            args=LAUNCH_ARGS,
            ignore_default_args=DROP_DEFAULT_ARGS,
            no_viewport=True,
            locale="zh-TW",
            timezone_id="Asia/Taipei",
        )
        try:
            yield ctx
        finally:
            await ctx.close()


async def webdriver_flag(context: BrowserContext) -> object:
    """`navigator.webdriver` as the page sees it. Used by the launch-flag test."""
    page = context.pages[0] if context.pages else await context.new_page()
    return await page.evaluate("() => navigator.webdriver")
