"""Setting the audience, against a page shaped like the one that defeated the model.

The fixture reproduces what the real run hit: the options are radios with no accessible name of
their own (the words sit in the row around them), 「只限本人」 is hidden behind 「更多選項」, and the
choice only takes effect when 「完成」 is pressed. A model clicking through that saw five rows all
called "on" and ended on the wrong audience with the post already typed.

So code does it. Deterministic, but never with a fixed selector: every look is a fresh snapshot
and every click uses a ref from that same look.

`test_it_refuses_when_it_cannot_be_set` is the floor underneath the fix - if the audience cannot
be made right, nothing is published, and the attempt stays provably un-fired so it is safe to
re-run.
"""
import asyncio

import pytest

import agent
import pagetools
from channels.facebook_page import FacebookPage, read_audience
from gate import Attempt, FireStage, settle

COMPOSER = """
<body style="margin:0;font:14px sans-serif">
<div role="dialog" aria-label="建立貼文" id="composer">
  <div role="textbox" contenteditable="true" style="min-height:40px">貼文內容</div>
  <button id="aud" onclick="openMenu()">貼文分享對象 朋友</button>
</div>
<div role="dialog" aria-label="貼文分享對象" id="menu" hidden
     style="width:320px;height:200px">
  <div id="rows" style="flex:1;overflow-y:auto"></div>
  <div id="moreBtn"><button onclick="expand()">更多選項</button></div>
  <button onclick="confirmPick()">完成</button>
</div>
<script>
  const SHORT = ['公開', '朋友'];
  const FULL  = ['公開', '朋友', '朋友，除了…', '特定朋友', '自訂', '家人', 'MORE_ONLY_ME'];
  let picked = '朋友';
  function draw(list){
    document.getElementById('rows').innerHTML = list.map(o => {
      const label = o === 'MORE_ONLY_ME' ? '只限本人' : o;
      // no aria-label, no text inside the control: the words are in the row
      return '<div style="padding:12px"><input type="radio" name="a"' +
             (label === picked ? ' checked' : '') +
             ' onclick="picked=\\'' + label + '\\'"><span>' + label + '</span></div>';
    }).join('');
  }
  function openMenu(){
    const m = document.getElementById('menu');
    // inline display would beat [hidden]{display:none}, so toggle it here instead
    m.hidden = false; m.style.display = 'flex'; m.style.flexDirection = 'column';
    draw(SHORT);
  }
  function expand(){ draw(FULL); document.getElementById('moreBtn').hidden = true; }
  function confirmPick(){
    const m = document.getElementById('menu');
    m.hidden = true; m.style.display = 'none';
    document.getElementById('aud').textContent = '貼文分享對象 ' + picked;
  }
  draw(SHORT);
</script>
</body>
"""


@pytest.fixture
def browser_page():
    """A real page, because the thing under test is how code reads and clicks a real DOM."""
    from playwright.async_api import async_playwright

    holder = {}

    async def start():
        holder["pw"] = await async_playwright().start()
        holder["browser"] = await holder["pw"].chromium.launch()
        page = await holder["browser"].new_page(viewport={"width": 500, "height": 400})
        await page.set_content(COMPOSER)
        return page

    async def stop():
        await holder["browser"].close()
        await holder["pw"].stop()

    loop = asyncio.new_event_loop()
    try:
        page = loop.run_until_complete(start())
    except Exception as exc:
        loop.close()
        pytest.skip(f"no usable chromium here: {type(exc).__name__}: {exc}")
    yield loop, page
    loop.run_until_complete(stop())
    loop.close()


def test_it_opens_the_menu_expands_it_picks_and_confirms(browser_page):
    loop, page = browser_page
    attempt = Attempt("facebook_page")

    before = read_audience(loop.run_until_complete(pagetools.snapshot(page, limit=400)))
    assert before == "朋友", "the fixture starts on the wrong audience on purpose"

    got = loop.run_until_complete(FacebookPage()._set_audience(page, "只限本人", attempt))

    assert got == "只限本人", attempt.steps
    after = read_audience(loop.run_until_complete(pagetools.snapshot(page, limit=400)))
    assert after == "只限本人", "the composer's own control must say so afterwards"
    assert any("更多選項" in s or "展開" in s for s in attempt.steps), attempt.steps


def test_the_option_was_unreadable_before_the_naming_fix(browser_page):
    """The control. These rows are exactly the shape that produced five options called "on"."""
    loop, page = browser_page
    loop.run_until_complete(page.evaluate("() => { openMenu(); expand(); }"))
    names = loop.run_until_complete(page.evaluate(
        """() => [...document.querySelectorAll('#rows input')].map(
             el => el.getAttribute('aria-label') || el.innerText || el.value)"""))
    assert set(names) == {"on"}, "if this stops being true the regression test above is pointless"


def test_it_refuses_when_it_cannot_be_set(browser_page, monkeypatch):
    """No such option on the page: nothing is published, and the attempt is still un-fired."""
    loop, page = browser_page
    import store
    monkeypatch.setattr(store, "get_setting", lambda key, default=None: "只有外星人")

    attempt = Attempt("facebook_page")
    with pytest.raises(agent.Refused) as caught:
        loop.run_until_complete(FacebookPage()._assert_audience(page, attempt))

    assert attempt.stage is FireStage.NOT_FIRED
    outcome = settle(attempt, caught.value)
    assert outcome.status == "failed" and outcome.retryable
    assert "只有外星人" in outcome.detail


def test_an_audience_that_is_already_right_is_left_alone(browser_page, monkeypatch):
    """The original sin: opening a menu that did not need opening, and breaking what was right."""
    loop, page = browser_page
    import store
    monkeypatch.setattr(store, "get_setting", lambda key, default=None: "朋友")

    opened = []
    real_click = pagetools.click

    async def spy(page_, ref, timeout=15_000):
        opened.append(ref)
        return await real_click(page_, ref, timeout)

    monkeypatch.setattr(pagetools, "click", spy)
    attempt = Attempt("facebook_page")
    loop.run_until_complete(FacebookPage()._assert_audience(page, attempt))

    assert opened == [], "nothing may be clicked when the audience is already right"
    assert any("朋友" in s for s in attempt.steps)


# ── where the proof comes from ───────────────────────────────────────────────────────────────
def test_verification_goes_to_the_destination_and_loads_it_fresh():
    """Reloading whatever the composer left behind is not proof.

    Facebook redirects the composer to /post/create, and a feed that already had the text on
    screen would "confirm" a post that was never made. The check has to NAVIGATE to the place
    the post was supposed to land, load it from the server, and read THAT.
    """
    import inspect

    from channels.facebook_page import FacebookPage

    src = inspect.getsource(FacebookPage._verify_published)
    assert "page.goto(check_url" in src, "it must navigate to where it checks, not reload in place"
    assert "page.reload(" not in src
    # The success path screenshots inside _found_it, the giving-up path inside _verify_published.
    # Both are "what it actually read", so both have to be looked at or this guard means nothing.
    assert "_shot(page" in src and "_shot(page" in inspect.getsource(FacebookPage._found_it),         "and screenshot what it actually read"


def test_the_agent_cannot_be_the_one_who_confirms():
    """The second look is a second LOOK, not a second opinion: `resume` can only add steps, and
    evidence still comes from the comparison in code. An earlier project's post step landed GREEN with
    {"post_url": null} once; this is the fence that was built after it."""
    import inspect

    from channels.facebook_page import FacebookPage

    verify = inspect.getsource(FacebookPage._verify_published)
    found = inspect.getsource(FacebookPage._found_it)

    # The two jobs are in two functions, and neither can do the other's. _verify_published is the
    # one that may wake the agent, so it is the one that may not prove anything; _found_it proves,
    # so it must have no way to ask the agent what it thinks.
    assert "operator.resume" in verify, "the second look still has to happen"
    assert "attempt.prove" not in verify, "whatever talks to the agent cannot produce evidence"
    assert "attempt.prove" in found and "operator" not in found and "resume" not in found,         "and whatever produces evidence cannot talk to the agent"

    # One `prove` per evidence kind, and no extra slipped in unreviewed.
    # One `prove` per comparison, and no third slipped in. Three tiers were deleted when they
    # were measured against the live site and found to be reading a DOM that does not exist.
    assert found.count("attempt.prove") == 2, "a new way to turn a look into evidence needs review"
    for kind in ("post_url", "feed_text_new"):
        assert f'"{kind}"' in found, kind


# A fan page's composer. A Page post is public, so Facebook draws no audience control at all -
# which is the shape that made 粉絲專頁 impossible to post to: the gate read "no audience" and
# refused every time.
PAGE_COMPOSER = """
<body style="margin:0;font:14px sans-serif">
<div role="dialog" aria-label="建立貼文">
  <div role="textbox" contenteditable="true" style="min-height:40px">貼文内容</div>
  <button>照片/影片</button>
  <button>發佈</button>
</div>
</body>
"""


def test_a_page_composer_has_nothing_to_choose_and_goes_through(browser_page, monkeypatch):
    """There is no 「貼文分享對象」 on a Page composer and there never will be. Refusing on its
    absence is refusing to post to a fan page at all."""
    loop, page = browser_page
    import store
    monkeypatch.setattr(store, "get_setting", lambda key, default=None: "只限本人")
    loop.run_until_complete(page.set_content(PAGE_COMPOSER))

    attempt = Attempt("facebook_page")
    loop.run_until_complete(FacebookPage()._assert_audience(page, attempt))   # must not raise

    assert attempt.stage is FireStage.NOT_FIRED, "it still has not published anything itself"
    assert any("粉絲專頁" in s for s in attempt.steps),         f"and it has to SAY why it skipped the check: {attempt.steps}"


def test_not_being_able_to_see_the_composer_is_not_a_page(browser_page, monkeypatch):
    """The failure mode of judging by absence: a page we cannot read looks exactly like a Page.
    Without the editor in the snapshot, absence is not evidence, so this must still refuse."""
    loop, page = browser_page
    import store
    monkeypatch.setattr(store, "get_setting", lambda key, default=None: "只限本人")
    loop.run_until_complete(page.set_content(
        "<body><div>正在載入…</div><button>重新整理</button></body>"))

    attempt = Attempt("facebook_page")
    with pytest.raises(agent.Refused) as caught:
        loop.run_until_complete(FacebookPage()._assert_audience(page, attempt))

    assert attempt.stage is FireStage.NOT_FIRED
    outcome = settle(attempt, caught.value)
    assert outcome.status == "failed" and outcome.retryable


# A 社團 composer, measured off the real page: the audience is STATED and the control is disabled,
# because members see a group post and that is not negotiable. Note the name carries the label
# words twice - 隱私設定 first, then 分享對象.
GROUP_COMPOSER = """
<body style="margin:0;font:14px sans-serif">
<div role="dialog" style="width:500px;height:449px">
  <div role="textbox" contenteditable="true" style="min-height:40px">貼文内容</div>
  <div role="button" aria-disabled="true" style="width:300px;height:30px"
       aria-label="編輯隱私設定。分享對象：test 的成員都看得到這則貼文。">
    <span>私密社團</span>
  </div>
  <div role="button" aria-label="發佈" style="width:90px;height:30px"></div>
</div>
</body>
"""


def test_the_last_label_in_the_name_is_the_one_that_names_the_value(browser_page):
    """「編輯隱私設定。分享對象：X」 matched 隱私設定 first, so the value read back as
    「。分享對象：X」 - a string that could never equal any audience we might want to set."""
    loop, page = browser_page
    loop.run_until_complete(page.set_content(GROUP_COMPOSER))

    got = read_audience(loop.run_until_complete(pagetools.snapshot(page, limit=400)))

    assert got == "test 的成員都看得到這則貼文", repr(got)


def test_an_audience_facebook_will_not_let_you_change_is_not_a_refusal(browser_page, monkeypatch):
    """A group's control is disabled. Clicking it anyway waited 15 seconds for a button that will
    never be enabled, and then refused to publish at all."""
    loop, page = browser_page
    import store
    monkeypatch.setattr(store, "get_setting", lambda key, default=None: "只限本人")
    loop.run_until_complete(page.set_content(GROUP_COMPOSER))

    clicked = []
    real_click = pagetools.click

    async def spy(page_, ref, timeout=15_000):
        clicked.append(ref)
        return await real_click(page_, ref, timeout)

    monkeypatch.setattr(pagetools, "click", spy)

    attempt = Attempt("facebook_page")
    loop.run_until_complete(FacebookPage()._assert_audience(page, attempt))   # must not raise

    assert clicked == [], f"a disabled control must not be clicked at all: {clicked}"
    assert attempt.stage is FireStage.NOT_FIRED
    assert any("不讓改" in s for s in attempt.steps), attempt.steps
