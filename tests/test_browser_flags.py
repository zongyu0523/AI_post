"""The launch flags, tested the way an earlier project tested its own:
against the REAL config (headful, your installed Chrome), with a control that proves the assertion
can fail.

Without the control this is a guard that cannot go red - it would pass just as happily if
`navigator.webdriver` were unreadable, or if the page never loaded. `test_control_*` launches the
same browser WITHOUT the two flags and asserts it IS flagged, so the first test's green means
something. (Verified: it reports True.)

Whether a browser can be launched at all is decided once, by a probe, and only that skips. The
tests themselves swallow nothing - a regression in our own config has to be able to fail.

Opens short-lived Chrome windows. Skip with:  python -m pytest tests -q -k "not flag"
"""
import asyncio

import pytest

from browser import DROP_DEFAULT_ARGS, LAUNCH_ARGS, open_context, webdriver_flag


@pytest.fixture(scope="module", autouse=True)
def browser_available(tmp_path_factory):
    async def probe():
        async with open_context(tmp_path_factory.mktemp("probe") / "p") as ctx:
            await webdriver_flag(ctx)

    try:
        asyncio.run(probe())
    except Exception as exc:
        pytest.skip(f"no usable Chrome here: {type(exc).__name__}: {exc}", allow_module_level=True)


def test_our_config_is_not_webdriver_flagged(tmp_path):
    async def go():
        async with open_context(tmp_path / "ours") as ctx:
            return await webdriver_flag(ctx)

    assert asyncio.run(go()) in (None, False)


def test_control_without_the_flags_it_is_flagged(tmp_path):
    """Remove the two lines from browser.py and the test above starts failing. That is the point."""
    from playwright.async_api import async_playwright

    async def go():
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                str(tmp_path / "default"), channel="chrome", headless=False)
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                return await page.evaluate("() => navigator.webdriver")
            finally:
                await ctx.close()

    assert asyncio.run(go()) is True, (
        "Playwright's defaults no longer set navigator.webdriver; re-check whether "
        f"{DROP_DEFAULT_ARGS} and {LAUNCH_ARGS} are still the right countermeasure")
