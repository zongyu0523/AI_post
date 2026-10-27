"""The outline has to say what a control IS, or the model clicks in the dark.

This is the regression for a real run that span in circles inside Facebook's「貼文分享對象」menu:
it opened the menu, clicked an option, closed it to read the result, found the wrong audience,
and went round again - about twenty turns, ending on the wrong setting.

The cause was in here, not in the model. Facebook's audience options are radio inputs with no
aria-label and no text of their own (the words are in the row drawn around them), so the old name
resolution fell all the way through to `el.value` - which for a radio with no value attribute is
the string "on". Five options, all called "on", and no way to see which one was already selected.

`test_the_old_naming_would_have_been_blind` is the control: it reproduces the old rule on the same
page and asserts it really does produce "on". Without it, the tests above could be green for a
reason that has nothing to do with the fix.
"""
import asyncio

import pytest

import pagetools

# Shaped like Facebook's composer: a modal, a row-labelled radio group, a <label for>, and an
# aria-labelledby - the four ways a real page hides the words from a naive reader.
MENU = """
<body style="font:14px sans-serif">
<div role="dialog" aria-label="建立貼文">
  <div role="textbox" contenteditable="true">貼文內容</div>
  <div role="button" aria-label="貼文分享對象 朋友">朋友</div>
  <div style="padding:4px"><div style="padding:4px"><input type="radio" name="a"><span>公開</span></div></div>
  <div style="padding:4px"><div style="padding:4px"><input type="radio" name="a" checked><span>朋友</span></div></div>
  <div style="padding:4px"><div style="padding:4px"><input type="radio" name="a"><span>只限本人</span></div></div>
  <input type="checkbox" id="c1"><label for="c1">同時分享到限時動態</label>
  <span id="lbl">加到你的珍藏</span>
  <div role="checkbox" aria-labelledby="lbl" aria-checked="false"
       style="width:18px;height:18px;border:1px solid #333"></div>
</div>
</body>
"""


@pytest.fixture(scope="module")
def outline():
    """One headless page, one snapshot, shared by every assertion below."""
    async def go():
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.set_content(MENU)
            snap = await pagetools.snapshot(page)
            await browser.close()
            return snap

    try:
        return asyncio.run(go())
    except Exception as exc:                                    # no browser on this machine
        pytest.skip(f"no usable chromium here: {type(exc).__name__}: {exc}",
                    allow_module_level=True)


def _named(snap, text):
    return [e for e in snap.elements if text in (e.get("name") or "")]


def test_a_row_labelled_radio_is_named_by_its_row(outline):
    """The words are in the row, not in the control. Three options, three different names."""
    names = [e["name"] for e in outline.elements if e["role"] == "radio"]
    for word in ("公開", "朋友", "只限本人"):
        assert any(word == n for n in names), f"{word} 不在 outline 的名字裡: {names}"


def test_a_valueless_radio_is_never_called_on(outline):
    """`.value` of a radio with no value attribute is "on". That is what the model was reading."""
    assert not _named(outline, "on"), outline.outline()


def test_the_outline_says_which_option_is_already_selected(outline):
    """Without this the only way to read the current setting is to click and find out."""
    chosen = [e for e in outline.elements if e.get("checked") is True]
    assert [e["name"] for e in chosen] == ["朋友"]
    assert "[SELECTED]" in outline.outline() and "[not selected]" in outline.outline()


def test_a_label_for_names_its_input(outline):
    assert _named(outline, "同時分享到限時動態")


def test_aria_labelledby_names_its_control(outline):
    hit = _named(outline, "加到你的珍藏")
    assert hit and hit[0]["checked"] is False


def test_the_old_naming_would_have_been_blind(outline):
    """The control. The old rule was:

        aria-label || placeholder || alt || title || innerText || value

    Applied to these same radios it yields "on" for every one of them - which is why the green
    above means something.
    """
    async def go():
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.set_content(MENU)
            names = await page.evaluate("""() => [...document.querySelectorAll('input[name=a]')]
                .map(el => el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
                           el.getAttribute('alt') || el.getAttribute('title') ||
                           el.innerText || el.value)""")
            await browser.close()
            return names

    assert asyncio.run(go()) == ["on", "on", "on"]


# ── the tree, and why it is a tree ───────────────────────────────────────────────────────────
TEXT_PAGE = """
<body style="font:14px sans-serif">
  <h2>建立貼文</h2>
  <p>這則貼文會發到你的個人動態。</p>
  <div><input type="radio" name="b"><span>第一個沒有名字的選項</span></div>
  <p>選好之後按下面那顆。</p>
  <button>發佈</button>
</body>
"""


def _tree(html):
    async def go():
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.set_content(html)
            snap = await pagetools.snapshot(page)
            await browser.close()
            return snap

    return asyncio.run(go())


def test_the_page_s_own_words_survive_into_the_outline():
    """The reason for the whole change. A flat list of interactive elements drops every word
    that is not inside a control, which is exactly where Facebook keeps the option labels."""
    out = _tree(TEXT_PAGE).outline()
    assert "text: 這則貼文會發到你的個人動態。" in out, out
    assert "text: 選好之後按下面那顆。" in out, out


def test_a_control_s_own_label_is_not_printed_twice():
    """A button's words are its name, not a separate line of text."""
    out = _tree(TEXT_PAGE).outline()
    assert out.count("發佈") == 1, out


def test_text_and_controls_keep_their_page_order():
    """Order is the information: it is what says WHICH control the words belong to."""
    lines = _tree(TEXT_PAGE).outline().splitlines()
    def at(fragment):
        return next(i for i, l in enumerate(lines) if fragment in l)
    assert at("這則貼文會發到") < at("radio") < at("選好之後") < at("發佈")


# Facebook's group composer, measured off the real page: the composer dialog is 500x449 and holds
# the editor, and NESTED inside it is a second role="dialog" - a 60px title bar carrying the words
# 「建立貼文」 and a close button, and nothing else.
NESTED = """
<body style="margin:0;font:14px sans-serif">
<div role="dialog" aria-label="通知" style="width:0;height:0;overflow:hidden">
  <button>開啟通知</button>
</div>
<div role="dialog" style="position:absolute;left:418px;top:180px;width:500px;height:449px">
  <div role="dialog" aria-label="建立貼文" style="width:500px;height:60px">
    <span>建立貼文</span>
    <div role="button" aria-label="關閉撰寫工具對話方塊" style="width:90px;height:30px"></div>
  </div>
  <div role="textbox" contenteditable="true" aria-label="寫點內容……"
       style="min-height:120px"></div>
  <div role="button" aria-label="相片／影片" style="width:90px;height:30px"></div>
  <div role="button" aria-label="發佈" style="width:90px;height:30px"></div>
</div>
</body>
"""


@pytest.fixture(scope="module")
def nested():
    async def go():
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport={"width": 1000, "height": 800})
            await page.set_content(NESTED)
            snap = await pagetools.snapshot(page, limit=400)
            moved = await pagetools.scroll(page)
            await browser.close()
            return snap, moved

    try:
        return asyncio.new_event_loop().run_until_complete(go())
    except Exception as exc:
        pytest.skip(f"no usable chromium here: {type(exc).__name__}: {exc}")


def test_a_nested_title_bar_is_not_the_dialog_to_scope_to(nested):
    """The bug this is the regression for: taking the LAST dialog in document order scoped the
    whole outline to a 60px title bar, so the model was shown a heading and a close button while
    the editor sat 449px below, invisible to it. It looked five times and scrolled twice."""
    snap, _ = nested

    assert snap.dialog, "a dialog IS open"
    roles = {e.get("role") for e in snap.elements}
    names = {e.get("name") for e in snap.elements}
    assert "textbox" in roles, f"the editor has to be in the outline: {snap.outline()}"
    assert "發佈" in names and "相片／影片" in names, names
    # scoping outward loses nothing: the title bar's own control is still there
    assert any("關閉" in (n or "") for n in names), names


def test_the_label_still_comes_from_the_nested_title_bar(nested):
    """The composer carries no aria-label of its own - the words are on the title bar - so the
    header line would go blank if it only ever read the dialog it scoped to."""
    snap, _ = nested
    assert snap.dialog_name == "建立貼文", snap.dialog_name


def test_an_offscreen_dialog_is_not_picked(nested):
    """通知 and Messenger sit in the DOM of every Facebook page as 0x0 dialogs."""
    snap, _ = nested
    assert not any("通知" in (e.get("name") or "") for e in snap.elements), snap.outline()


def test_scrolling_with_no_ref_does_not_fall_through_to_the_page(nested):
    """The scroller had the same last-dialog bug AND no visibility filter, so it landed on the
    title bar, found nothing scrollable, and scrolled the feed behind the modal instead - the
    model scrolled, the modal did not move, and the outline came back identical."""
    _, moved = nested
    assert moved["moved"] == 0 and not moved["inList"],         f"nothing in this fixture scrolls, so the honest answer is 0: {moved}"
