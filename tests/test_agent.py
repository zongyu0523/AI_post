"""The fences around the model.

The model drives the browser because a browser job cannot be a list of fixed steps. These are the
things it is nevertheless not allowed to do, each one tested by behaviour rather than by shape.
"""
import pytest

import agent
from channels.facebook_page import resolve_target
from gate import Attempt


# ── tool-argument mangling ───────────────────────────────────────────────────────────────────
def test_parse_args_accepts_a_plain_dict():
    assert agent.parse_args({"ref": "e3"}) == {"ref": "e3"}


def test_parse_args_accepts_a_json_string():
    assert agent.parse_args('{"ref": "e3", "why": "點開編輯器"}')["ref"] == "e3"


def test_parse_args_unwraps_the_relay_text_envelope():
    """Seen in the wild: a provider wrapped arguments as
    {"$text": "<json string>"}. Unwrapped at the tool boundary so the loop does not burn turns."""
    assert agent.parse_args({"$text": '{"ref": "e7"}'}) == {"ref": "e7"}


def test_parse_args_survives_garbage():
    assert agent.parse_args("not json at all") == {}
    assert agent.parse_args(None) == {}


# ── what the model is offered ────────────────────────────────────────────────────────────────
def _names(tools):
    return {t["function"]["name"] for t in tools}


def test_publish_is_not_offered_during_a_dry_run():
    dry = agent.BrowserAgent(cfg=None, page=None, post=None, attempt=Attempt("fb"), dry_run=True)
    assert "publish" not in _names(dry.tools)


def test_publish_is_offered_on_a_real_run():
    real = agent.BrowserAgent(cfg=None, page=None, post=None, attempt=Attempt("fb"), dry_run=False)
    assert "publish" in _names(real.tools)


def test_type_body_takes_no_text_argument():
    """The model must not be able to retype, paraphrase or translate the user's post."""
    spec = next(t for t in agent.TOOLS_BASE if t["function"]["name"] == "type_body")
    assert "text" not in spec["function"]["parameters"]["properties"]


def test_publish_words_catch_the_usual_labels():
    for label in ["發佈", "發布", "Post", "Publish", "分享", "发布"]:
        assert agent.PUBLISH_WORDS.search(label), label
    for label in ["照片/影片", "取消", "Cancel", "標記朋友", "貼文分享對象 只限本人"]:
        assert not agent.PUBLISH_WORDS.search(label), label


# ── stale outlines are actively removed ──────────────────────────────────────────────────────
def test_old_outlines_are_expired_so_stale_refs_cannot_be_shopped_for():
    messages = [{"role": "system", "content": "s"}]
    for i in range(5):
        messages.append({"role": "assistant", "content": None})
        messages.append({"role": "tool", "tool_call_id": str(i), "content": f"outline {i}"})
    agent._expire_old_outlines(messages, keep=2)

    tools = [m for m in messages if m["role"] == "tool"]
    assert tools[-1]["content"] == "outline 4"
    assert tools[-2]["content"] == "outline 3"
    assert all(m["content"].startswith("(expired") for m in tools[:-2])


# ── destination ──────────────────────────────────────────────────────────────────────────────
def test_empty_target_means_your_own_timeline():
    """The personal timeline is a real destination - it is the one with no API at all, which is
    why the browser is the only way to reach it."""
    url, where = resolve_target("")
    assert url == "https://www.facebook.com/"
    assert "個人動態" in where


def test_a_bare_page_name_is_expanded():
    url, where = resolve_target("mypage")
    assert url == "https://www.facebook.com/mypage"
    assert "粉絲專頁" in where


def test_a_full_page_url_is_kept():
    url, where = resolve_target("https://www.facebook.com/mypage/")
    assert url == "https://www.facebook.com/mypage"
    assert "粉絲專頁" in where


def test_the_home_feed_url_is_labelled_as_the_timeline():
    """Not refused - just named honestly, so the agent is told which destination it was given."""
    for raw in ["https://www.facebook.com/", "https://facebook.com/home.php"]:
        assert "個人動態" in resolve_target(raw)[1], raw


# ── the loop guard ───────────────────────────────────────────────────────────────────────────
def test_the_third_click_on_the_same_thing_is_refused(monkeypatch):
    """A real run span ~20 turns re-opening Facebook's audience menu and clicking the same
    nameless options. Older outlines are expired from the context, so the model cannot see that
    it already tried this - the loop has to be ended from the outside.

    Keyed on the element's NAME, not its ref: refs are reborn by every snapshot, so the same
    button is a different ref each turn and nothing would ever look repeated.
    """
    import asyncio

    import pagetools

    snap = pagetools.Snapshot(url="u", title="t", dialog=True, dialog_name="建立貼文",
                              elements=({"ref": "e1", "role": "input", "name": "只限本人"},))
    clicks = []

    async def fake_snapshot(page, limit=110):
        return snap

    async def fake_click(page, ref, timeout=0):
        clicks.append(ref)

    monkeypatch.setattr(pagetools, "snapshot", fake_snapshot)
    monkeypatch.setattr(pagetools, "click", fake_click)

    operator = agent.BrowserAgent(cfg=None, page=object(), post=None,
                                  attempt=Attempt("fb"), dry_run=True)
    operator.last = snap

    async def press():
        return [await operator._dispatch("click", {"ref": "e1", "why": "試試看"})
                for _ in range(3)]

    results = asyncio.run(press())
    assert clicks == ["e1", "e1"], "the third press must not reach the page"
    assert "twice" in results[2] and "give_up" in results[2]


def test_clicking_different_things_is_not_blocked(monkeypatch):
    """The control: the guard must not stop an agent that is actually getting somewhere."""
    import asyncio

    import pagetools

    snap = pagetools.Snapshot(url="u", title="t", dialog=False, dialog_name="",
                              elements=tuple({"ref": f"e{i}", "role": "button", "name": f"第{i}個"}
                                             for i in range(1, 5)))
    clicks = []

    async def fake_snapshot(page, limit=110):
        return snap

    async def fake_click(page, ref, timeout=0):
        clicks.append(ref)

    monkeypatch.setattr(pagetools, "snapshot", fake_snapshot)
    monkeypatch.setattr(pagetools, "click", fake_click)

    operator = agent.BrowserAgent(cfg=None, page=object(), post=None,
                                  attempt=Attempt("fb"), dry_run=True)
    operator.last = snap

    async def press():
        for i in range(1, 5):
            await operator._dispatch("click", {"ref": f"e{i}", "why": "下一步"})

    asyncio.run(press())
    assert clicks == ["e1", "e2", "e3", "e4"]


# ── the refs the model answers with ──────────────────────────────────────────────────────────
def test_an_act_resolves_against_the_outline_the_model_was_shown(monkeypatch):
    """The bug behind 「點開『貼文分享對象』→『編輯相片』」.

    `click` used to take a FRESH snapshot and look the ref up in that. Every snapshot re-stamps
    `data-aipost-ref` from scratch, so if the page changed in between, e2 was now a different
    element - and it clicked it. The ref must mean what it meant in the outline the model read.
    """
    import asyncio

    import pagetools

    shown = pagetools.Snapshot(url="u", title="t", dialog=True, dialog_name="建立貼文",
                               elements=({"ref": "e1", "role": "button", "name": "照片/影片"},
                                         {"ref": "e2", "role": "button",
                                          "name": "貼文分享對象 只限本人"}))
    # what a fresh snapshot would say AFTER an upload shifted everything along
    after = pagetools.Snapshot(url="u", title="t", dialog=True, dialog_name="建立貼文",
                               elements=({"ref": "e1", "role": "button", "name": "照片/影片"},
                                         {"ref": "e2", "role": "button", "name": "編輯相片"}))
    clicks = []

    async def fake_snapshot(page, limit=110):
        return after

    async def fake_click(page, ref, timeout=0):
        clicks.append(ref)

    monkeypatch.setattr(pagetools, "snapshot", fake_snapshot)
    monkeypatch.setattr(pagetools, "click", fake_click)

    operator = agent.BrowserAgent(cfg=None, page=object(), post=None,
                                  attempt=Attempt("fb"), dry_run=True)
    operator.last = shown
    asyncio.run(operator._dispatch("click", {"ref": "e2", "why": "確認分享對象"}))

    assert clicks == ["e2"]
    assert any("貼文分享對象" in s for s in operator.attempt.steps), operator.attempt.steps
    assert not any("編輯相片" in s for s in operator.attempt.steps), \
        "the step named an element the model never saw - the ref was resolved in the wrong generation"


def test_a_ref_from_no_outline_at_all_is_refused(monkeypatch):
    import asyncio

    import pagetools

    empty = pagetools.Snapshot(url="u", title="t", dialog=False, dialog_name="", elements=())

    async def fake_snapshot(page, limit=110):
        return empty

    monkeypatch.setattr(pagetools, "snapshot", fake_snapshot)
    operator = agent.BrowserAgent(cfg=None, page=object(), post=None,
                                  attempt=Attempt("fb"), dry_run=True)
    operator.last = empty
    with pytest.raises(pagetools.StaleRef):
        asyncio.run(operator._dispatch("click", {"ref": "e9", "why": "亂猜一個"}))


def test_there_is_a_way_to_scroll():
    """The system prompt told the model to scroll; there was no scroll tool. A list longer than
    the window simply did not exist."""
    assert "scroll" in _names(agent.TOOLS_BASE)
    assert "scroll" in agent.SYSTEM


# ── the guard that runs before publish ───────────────────────────────────────────────────────
def test_the_guard_can_refuse_before_anything_fires(monkeypatch):
    """A refusal must land while the attempt is still NOT_FIRED, so the gate reads `failed`
    (safe to re-run) rather than `uncertain` (a human has to go and look)."""
    import asyncio

    import pagetools
    from gate import FireStage, settle

    snap = pagetools.Snapshot(url="u", title="t", dialog=True, dialog_name="建立貼文",
                              elements=({"ref": "e1", "role": "button", "name": "發佈"},))
    clicks = []

    async def fake_snapshot(page, limit=110):
        return snap

    async def fake_click(page, ref, timeout=0):
        clicks.append(ref)

    async def refuse():
        raise agent.Refused("分享對象是「朋友」，不是「只限本人」，所以沒有發")

    monkeypatch.setattr(pagetools, "snapshot", fake_snapshot)
    monkeypatch.setattr(pagetools, "click", fake_click)

    attempt = Attempt("facebook_page")
    operator = agent.BrowserAgent(cfg=None, page=object(), post=None, attempt=attempt,
                                  dry_run=False, guard=refuse)
    operator.last = snap

    with pytest.raises(agent.Refused) as caught:
        asyncio.run(operator._dispatch("publish", {"ref": "e1", "why": "發布"}))

    assert clicks == [], "nothing may be pressed after the guard refused"
    assert attempt.stage is FireStage.NOT_FIRED
    outcome = settle(attempt, caught.value)
    assert outcome.status == "failed" and outcome.retryable
    assert "只限本人" in outcome.detail


# ── the audience is read, never negotiated ───────────────────────────────────────────────────
def test_the_audience_is_read_off_the_control_itself():
    """Facebook states the setting inside the control's name, so nothing has to be opened."""
    import pagetools
    from channels.facebook_page import read_audience

    def snap(*names):
        return pagetools.Snapshot(url="", title="", dialog=True, dialog_name="建立貼文",
                                  elements=tuple({"ref": f"e{i}", "role": "button", "name": n}
                                                 for i, n in enumerate(names, 1)))

    assert read_audience(snap("編輯相片", "貼文分享對象 只限本人")) == "只限本人"
    assert read_audience(snap("Audience: Friends")) == "Friends"
    assert read_audience(snap("照片/影片", "標記朋友")) is None
    # the open menu's own heading is not a value: reading it back would turn "the menu is still
    # open" into an audience, and the post would be refused for the wrong reason
    assert read_audience(snap("貼文分享對象", "完成")) is None
    assert read_audience(snap("貼文分享對象", "貼文分享對象 公開")) == "公開"


def test_the_task_tells_the_agent_to_leave_the_audience_alone():
    """The loop was in that menu. The instruction that sent it there is gone."""
    from store import Post
    from channels.facebook_page import FacebookPage

    goal = FacebookPage()._goal("https://www.facebook.com/", "你的個人動態",
                                Post(1, "t", "body", ()), dry_run=False)
    assert "Do NOT open that menu" in goal
    assert "更多選項" in goal                       # named, so it is recognisably off limits
    assert "choose the option named" not in goal    # the instruction that caused the loop


# ── after publish, the operator's turn is over ───────────────────────────────────────────────
def test_pressing_publish_ends_the_run(monkeypatch):
    """The real run that prompted this spent 12 of its 26 steps scrolling a timeline up and down
    looking for the post it had just made, then died on the step cap.

    Hunting for the post is work the operator is bad at - a feed is infinite and the outline is
    one window of it - and work the channel already does properly, by navigating to the
    destination and reading the whole page text. So publishing is the last act.
    """
    import asyncio

    import pagetools

    snap = pagetools.Snapshot(url="u", title="t", dialog=True, dialog_name="建立貼文",
                              elements=({"ref": "e1", "role": "button", "name": "發佈"},))
    clicks = []

    async def fake_snapshot(page, limit=220):
        return snap

    async def fake_click(page, ref, timeout=0):
        clicks.append(ref)

    monkeypatch.setattr(pagetools, "snapshot", fake_snapshot)
    monkeypatch.setattr(pagetools, "click", fake_click)

    attempt = Attempt("facebook_page")
    operator = agent.BrowserAgent(cfg=None, page=object(), post=None, attempt=attempt,
                                  dry_run=False)
    operator.last = snap

    with pytest.raises(agent.Stop) as stop:
        asyncio.run(operator._dispatch("publish", {"ref": "e1", "why": "發布"}))

    assert stop.value.ok is True
    assert clicks == ["e1"], "it still presses the button - it just stops afterwards"
    assert attempt.stage.name == "UNKNOWN", "and the gate knows something may have fired"


def test_the_task_does_not_send_it_looking_for_the_post():
    from store import Post
    from channels.facebook_page import FacebookPage

    goal = FacebookPage()._goal("https://www.facebook.com/", "你的個人動態",
                                Post(1, "t", "body", ()), dry_run=False)
    assert "LAST thing you do" in goal
    assert "do not scroll the feed" in goal
    assert "look for the platform's own confirmation" not in goal


def test_scrolling_in_circles_is_stopped(monkeypatch):
    """`moved == 0` ends a scroll at the bottom of a menu, but a timeline always moves, so that
    check can never end a feed hunt. Consecutive scrolls are what has to be counted."""
    import asyncio

    import pagetools

    snap = pagetools.Snapshot(url="u", title="t", dialog=False, dialog_name="",
                              elements=({"ref": "e1", "role": "button", "name": "貼文"},),
                              lines=('- button "貼文" [ref=e1]',))
    scrolled = []

    async def fake_snapshot(page, limit=220):
        return snap

    async def fake_scroll(page, ref=None, down=True):
        scrolled.append(down)
        return {"moved": 600, "inList": False}          # an infinite feed always moves

    async def fake_click(page, ref, timeout=0):
        pass

    monkeypatch.setattr(pagetools, "snapshot", fake_snapshot)
    monkeypatch.setattr(pagetools, "scroll", fake_scroll)
    monkeypatch.setattr(pagetools, "click", fake_click)

    operator = agent.BrowserAgent(cfg=None, page=object(), post=None,
                                  attempt=Attempt("fb"), dry_run=True)
    operator.last = snap

    async def go():
        out = []
        for i in range(6):
            out.append(await operator._dispatch(
                "scroll", {"direction": "down" if i % 2 else "up", "why": "找貼文"}))
        return out

    results = asyncio.run(go())
    assert len(scrolled) == 3, "it may scroll a few times, then it is told to stop"
    assert "scrolled" in results[-1] and "give_up" in results[-1]

    # doing something else clears it: the guard is against looping, not against scrolling
    asyncio.run(operator._dispatch("click", {"ref": "e1", "why": "點點看"}))
    asyncio.run(operator._dispatch("scroll", {"direction": "down", "why": "再看一下"}))
    assert len(scrolled) == 4


def test_a_group_url_is_not_called_a_fan_page():
    """A 社團 is a third kind of destination. The label is the one sentence telling the agent
    where it is supposed to be, so calling a group a page puts a wrong word in the task."""
    url, where = resolve_target("https://www.facebook.com/groups/123456789012345")
    assert url == "https://www.facebook.com/groups/123456789012345"
    assert where == "社團 123456789012345", where
    assert "粉絲專頁" not in where


def test_looking_at_the_same_unchanged_screen_is_stopped(monkeypatch):
    """The gap the scroll guard left. `scrolls` counts CONSECUTIVE scrolls, so
    look-scroll-look-look-scroll-look never trips it - which is exactly the shape of the run that
    sat in a group composer taking the same snapshot over and over.

    The screen itself is the evidence: identical outline, identical situation."""
    import asyncio

    import pagetools

    snap = pagetools.Snapshot(url="u", title="t", dialog=True, dialog_name="建立貼文",
                              elements=({"ref": "e1", "role": "button", "name": "關閉"},),
                              lines=('- button "關閉" [ref=e1]',))

    async def fake_snapshot(page, limit=220):
        return snap

    async def fake_scroll(page, ref=None, down=True):
        return {"moved": 600, "inList": True}

    monkeypatch.setattr(pagetools, "snapshot", fake_snapshot)
    monkeypatch.setattr(pagetools, "scroll", fake_scroll)

    operator = agent.BrowserAgent(cfg=None, page=object(), post=None,
                                  attempt=Attempt("fb"), dry_run=True)

    async def go():
        seen = []
        for i in range(8):
            # a scroll every third turn, so the consecutive-scroll guard never fires
            if i and i % 3 == 0:
                await operator._dispatch("scroll", {"direction": "down", "why": "找輸入框"})
            seen.append(await operator._dispatch("snapshot", {"why": "再看一次"}))
        return seen

    with pytest.raises(agent.Stop) as caught:
        asyncio.run(go())

    assert not caught.value.ok, "an unchanged screen is not a success"
    assert "沒變" in caught.value.message, caught.value.message
    # and it complained before it gave up, rather than only at the end
    assert any("一模一樣" in s for s in operator.attempt.steps), operator.attempt.steps


def test_a_screen_that_does_change_is_never_nagged(monkeypatch):
    """The guard must not fire on a run that is making progress, or it would break every post."""
    import asyncio

    import pagetools

    calls = {"n": 0}

    async def fake_snapshot(page, limit=220):
        calls["n"] += 1
        name = f"button {calls['n']}"
        return pagetools.Snapshot(url="u", title="t", dialog=False, dialog_name="",
                                  elements=({"ref": "e1", "role": "button", "name": name},),
                                  lines=(f'- button "{name}" [ref=e1]',))

    monkeypatch.setattr(pagetools, "snapshot", fake_snapshot)
    operator = agent.BrowserAgent(cfg=None, page=object(), post=None,
                                  attempt=Attempt("fb"), dry_run=True)

    async def go():
        for _ in range(8):
            await operator._dispatch("snapshot", {"why": "look"})

    asyncio.run(go())          # must not raise
    assert operator.same_views == 0
    assert not any("一模一樣" in s for s in operator.attempt.steps)


# ── the body is not the model's to touch ─────────────────────────────────────────────────────
def test_the_read_back_accepts_an_editor_that_shows_emoji_as_images(monkeypatch):
    """Facebook draws emoji as <img>, so 「ETETEGEG2️⃣4️⃣6️⃣6️⃣」 reads back as 「ETETEGEG」 and
    always will. Demanding character-equality made the warning fire on every post with an emoji,
    and the model obeyed it by retyping the body WITHOUT them - deleting what the user wrote."""
    import asyncio

    import pagetools
    from store import Post

    body = "ETETEGEG2\ufe0f\u20e34\ufe0f\u20e36\ufe0f\u20e36\ufe0f\u20e3"
    snap = pagetools.Snapshot(url="u", title="t", dialog=True, dialog_name="建立貼文",
                              elements=({"ref": "e1", "role": "textbox", "name": "",
                                         "value": "ETETEGEG"},),
                              lines=('- textbox (value: "ETETEGEG") [ref=e1]',))

    async def fake_snapshot(page, limit=220):
        return snap

    async def fake_fill(page, ref, text, timeout=0):
        pass

    monkeypatch.setattr(pagetools, "snapshot", fake_snapshot)
    monkeypatch.setattr(pagetools, "fill", fake_fill)

    operator = agent.BrowserAgent(cfg=None, page=object(), post=Post(1, "t", body, ()),
                                  attempt=Attempt("fb"), dry_run=False)
    operator.last = snap
    out = asyncio.run(operator._dispatch("type_body", {"ref": "e1", "why": "打內文"}))

    assert "WARNING" not in out, out[:200]
    assert "Do not retype it" in out


def test_the_model_may_not_overwrite_the_body_it_was_given(monkeypatch):
    """The guard under the wording. A real run typed the body, read it back, decided the emoji
    were extra characters, and overwrote the editor with a stripped copy."""
    import asyncio

    import pagetools
    from store import Post

    snap = pagetools.Snapshot(url="u", title="t", dialog=True, dialog_name="建立貼文",
                              elements=({"ref": "e1", "role": "textbox", "name": "",
                                         "value": "ETETEGEG"},),
                              lines=('- textbox [ref=e1]',))
    filled = []

    async def fake_snapshot(page, limit=220):
        return snap

    async def fake_fill(page, ref, text, timeout=0):
        filled.append(text)

    monkeypatch.setattr(pagetools, "snapshot", fake_snapshot)
    monkeypatch.setattr(pagetools, "fill", fake_fill)

    operator = agent.BrowserAgent(cfg=None, page=object(), post=Post(1, "t", "x", ()),
                                  attempt=Attempt("fb"), dry_run=False)
    operator.last = snap
    operator.body_typed = True
    out = asyncio.run(operator._dispatch("type", {"ref": "e1", "text": "ETETEGEG", "why": "覆寫"}))

    assert "REFUSED" in out, out[:200]
    assert filled == [], f"it must not reach the editor at all: {filled}"


def test_a_button_may_still_be_typed_into_after_the_body(monkeypatch):
    """The guard is about the editor, not about every tool call - this must not break the rest."""
    import asyncio

    import pagetools
    from store import Post

    snap = pagetools.Snapshot(url="u", title="t", dialog=False, dialog_name="",
                              elements=({"ref": "e1", "role": "searchbox", "name": "搜尋"},),
                              lines=('- searchbox "搜尋" [ref=e1]',))
    filled = []

    async def fake_snapshot(page, limit=220):
        return snap

    async def fake_fill(page, ref, text, timeout=0):
        filled.append(text)

    monkeypatch.setattr(pagetools, "snapshot", fake_snapshot)
    monkeypatch.setattr(pagetools, "fill", fake_fill)

    operator = agent.BrowserAgent(cfg=None, page=object(), post=Post(1, "t", "x", ()),
                                  attempt=Attempt("fb"), dry_run=False)
    operator.last = snap
    operator.body_typed = True
    asyncio.run(operator._dispatch("type", {"ref": "e1", "text": "abc", "why": "搜尋"}))

    assert filled == ["abc"]


def test_the_task_never_shows_the_model_the_emoji_stripped_body():
    """Where it got the idea. The task used to carry `post.marker` as a 'preview' of the body -
    which is the body with the emoji taken out, because that is what a marker is for."""
    from channels.facebook_page import FacebookPage
    from store import Post

    post = Post(1, "t", "ETETEGEG2\ufe0f\u20e34\ufe0f\u20e36\ufe0f\u20e36\ufe0f\u20e3", ())
    assert post.marker == "ETETEGEG", "the stripped form has to be different from the body"

    goal = FacebookPage()._goal("u", "社團 test", post, False)

    assert post.marker not in goal, "the stripped body must not be shown as the text to post"
    assert "never retype it" in goal.lower()
