"""Proving a post went out, against the page Facebook actually serves.

Everything here replaces a verification built on `div[role="article"]` and on reading 「剛剛」 off
a post's header. Measured on the live site, both are fiction:

  * a group page contains ZERO `div[role="article"]`, and a profile's are empty shells;
  * the timestamp is split across decoy spans and is not retrievable as text;
  * a group feed is sorted 「最相關」, so a brand-new post need not be near the top at all.

That machinery passed its tests only because the fixtures were written to match it. So the
question is no longer "what does our post look like" - every answer to that was a guess that
turned out wrong - but "what appeared here that was not here before".

Two comparisons, both on strings, which is all an obfuscated DOM leaves standing:

  * the post ids Facebook carries in its own links (four photos of one post share
    `set=pcb.<id>`), which needs no readable text;
  * the page's text, which needs no readable ids.

`test_without_a_baseline_nothing_can_be_proved` is the floor under both: an empty baseline means
"we never read the page", not "the page was empty", and against nothing everything looks new.
"""
import asyncio

import pytest

from channels.facebook_page import FacebookPage, verify_url
from gate import Attempt
from store import Post

# A group page as served: no role="article" anywhere, obfuscated wrappers, the post's identity
# carried only in its photo links. The notification dropdown is the trap - every Facebook page
# has one, it is full of OTHER people's post ids, and its contents change between loads.
GROUP_PAGE = """
<body style="margin:0;font:14px sans-serif">
<div role="dialog" aria-label="通知" style="width:0;height:0;overflow:hidden;display:none">
  <a href="https://www.facebook.com/another.member.000/posts/pfbid0000000000000000000000000000">
    別的成員新增了相片</a>
</div>
<div class="html-div xdj266r">
  <div class="html-div x1lziwa"><span>發文者</span><span>test</span></div>
  <div class="html-div x1lziwa"><span>上週的特價 快來買</span></div>
  <a href="https://www.facebook.com/photo/?fbid=111111&set=pcb.900000000000001"
     style="display:block;width:80px;height:60px">相片</a>
  <a href="https://www.facebook.com/photo/?fbid=111112&set=pcb.900000000000001"
     style="display:block;width:80px;height:60px">相片</a>
</div>
NEWPOST
</body>
"""

# What a new post with photos adds to it.
WITH_PHOTOS = """
<div class="html-div xdj266r">
  <div class="html-div x1lziwa"><span>發文者</span><span>test</span></div>
  <div class="html-div x1lziwa"><span>BODYTEXT</span></div>
  <a href="https://www.facebook.com/photo/?fbid=222221&set=pcb.900000000000002"
     style="display:block;width:80px;height:60px">相片</a>
  <a href="https://www.facebook.com/photo/?fbid=222222&set=pcb.900000000000002"
     style="display:block;width:80px;height:60px">相片</a>
</div>
"""

# An older post that this load rendered and the baseline load did not: its ids are BELOW
# everything already seen, because Facebook's ids climb with time.
OLDER_POST = """
<div class="html-div xdj266r">
  <div class="html-div x1lziwa"><span>去年的舊文</span></div>
  <a href="https://www.facebook.com/photo/?fbid=100001&set=pcb.800000000000001"
     style="display:block;width:80px;height:60px">相片</a>
</div>
"""

# And what a text-only post adds: words, and no new id at all.
TEXT_ONLY = """
<div class="html-div xdj266r">
  <div class="html-div x1lziwa"><span>BODYTEXT</span></div>
</div>
"""


@pytest.fixture
def browser_page():
    """A real page: what is under test is how code reads a real DOM."""
    from playwright.async_api import async_playwright

    holder = {}

    async def start():
        holder["pw"] = await async_playwright().start()
        holder["browser"] = await holder["pw"].chromium.launch()
        return await holder["browser"].new_page(viewport={"width": 600, "height": 700})

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


def _html(block: str | None, body: str = "") -> str:
    return GROUP_PAGE.replace("NEWPOST", block.replace("BODYTEXT", body) if block else "")


def _baseline(loop, page, monkeypatch, tmp_path) -> dict:
    """The page before publishing - read exactly the way the channel reads it."""
    import channels.facebook_page as fb

    monkeypatch.setattr(fb, "SHOTS_DIR", tmp_path)
    loop.run_until_complete(page.set_content(_html(None)))
    return loop.run_until_complete(FacebookPage()._before_state(page, Attempt("facebook_page")))


def _look(loop, page, body: str, before: dict, block: str | None):
    """Load the page as it is after publishing, then ask whether our post is on it."""
    loop.run_until_complete(page.set_content(_html(block, body)))
    post = Post(1, "t", body, ())
    attempt = Attempt("facebook_page")
    found = loop.run_until_complete(
        FacebookPage()._found_it(page, post, attempt, "x", "社團 test", post.marker[:20], before))
    return found, attempt


# ── where we go to look ──────────────────────────────────────────────────────────────────────
def test_a_group_is_verified_on_the_page_that_lists_only_my_posts():
    """A group feed is sorted 「最相關」 and full of other members' posts, so "something appeared
    there" would not mean we put it there."""
    got = verify_url("https://www.facebook.com/groups/123456789012345", "100000000000777")
    assert got == "https://www.facebook.com/groups/123456789012345/user/100000000000777/"


def test_a_page_or_profile_is_verified_where_it_posted():
    """Those already show only their own posts, so there is nowhere better to go."""
    assert verify_url("https://www.facebook.com/myshop", "1") == "https://www.facebook.com/myshop"
    assert verify_url("https://www.facebook.com/", "1") == "https://www.facebook.com/"


def test_without_the_uid_a_group_falls_back_to_the_group_itself():
    """No c_user cookie is a reason to look somewhere worse, not to crash."""
    assert verify_url("https://www.facebook.com/groups/123", None) == \
        "https://www.facebook.com/groups/123"


# ── the baseline ─────────────────────────────────────────────────────────────────────────────
def test_the_baseline_reads_both_ids_and_text(browser_page, monkeypatch, tmp_path):
    loop, page = browser_page
    before = _baseline(loop, page, monkeypatch, tmp_path)

    assert set(before["ids"]) == {"111111", "111112", "900000000000001"}, before["ids"]
    assert "快來買" in before["text"]


def test_a_closed_notification_panel_is_not_part_of_the_baseline(browser_page,
                                                                 monkeypatch, tmp_path):
    """Measured on the live site: the only permalink-carrying links on a group page came from the
    notification dropdown, and they are other people's. They change between loads, so counting
    them would make a stranger's new notification look like our post."""
    loop, page = browser_page
    before = _baseline(loop, page, monkeypatch, tmp_path)

    assert not any(i.startswith("pfbid") for i in before["ids"]), before["ids"]


# ── what gets proved ─────────────────────────────────────────────────────────────────────────
def test_a_post_id_that_was_not_there_before_is_the_proof(browser_page, monkeypatch, tmp_path):
    """The one that survives an all-emoji body: no text is read at all, only Facebook's own id
    for a post that did not exist a minute ago."""
    loop, page = browser_page
    before = _baseline(loop, page, monkeypatch, tmp_path)
    assert Post(1, "t", "🎉🎉🎉", ()).marker == "", "there must be no text to fall back on"

    found, attempt = _look(loop, page, "🎉🎉🎉", before, WITH_PHOTOS)

    assert found and attempt.evidence.kind == "post_url", attempt.steps
    assert "900000000000002" in attempt.evidence.value


def test_text_that_was_not_there_before_proves_it_when_there_is_no_new_id(browser_page,
                                                                          monkeypatch, tmp_path):
    """A post with no photos carries no `pcb.` id, so the words have to do it. 「gegege」 is six
    characters and would be far too short to trust without a baseline."""
    loop, page = browser_page
    before = _baseline(loop, page, monkeypatch, tmp_path)
    body = "gegege3️⃣4️⃣5️⃣6️⃣7️⃣"
    assert Post(1, "t", body, ()).marker == "gegege"

    found, attempt = _look(loop, page, body, before, TEXT_ONLY)

    assert found and attempt.evidence.kind == "feed_text_new", attempt.steps
    assert attempt.evidence.value == "gegege"


def test_styled_letters_are_searchable_text_not_pictures(browser_page, monkeypatch, tmp_path):
    """𝗍𝗲𝘁𝘀𝗲𝘁 is Mathematical Alphanumeric Symbols and Facebook renders it as the letters it is.
    Stripping it as if it were emoji took a real post's needle down to the one character "t"."""
    body = "𝗍𝗲𝘁𝘀𝗲𝘁𝘀𝗲t2️⃣5️⃣6️⃣6️⃣"
    assert Post(1, "t", body, ()).marker == "𝗍𝗲𝘁𝘀𝗲𝘁𝘀𝗲t", repr(Post(1, "t", body, ()).marker)

    loop, page = browser_page
    before = _baseline(loop, page, monkeypatch, tmp_path)

    found, attempt = _look(loop, page, body, before, TEXT_ONLY)

    assert found and attempt.evidence.kind == "feed_text_new", attempt.steps


def test_nothing_new_proves_nothing(browser_page, monkeypatch, tmp_path):
    """The page is exactly as it was: the post did not land, and nothing may be claimed."""
    loop, page = browser_page
    before = _baseline(loop, page, monkeypatch, tmp_path)

    found, attempt = _look(loop, page, "今天新貨到了", before, None)

    assert not found and attempt.evidence is None, attempt.steps


def test_words_that_were_already_there_are_not_new(browser_page, monkeypatch, tmp_path):
    """「特價2️⃣4️⃣5️⃣ 快來買」 leaves 「快來買」, which last week's post also says. The baseline is
    what catches this; the old whole-page search called it a success."""
    loop, page = browser_page
    before = _baseline(loop, page, monkeypatch, tmp_path)
    assert Post(1, "t", "特價2️⃣4️⃣5️⃣ 快來買", ()).marker == "快來買"

    found, attempt = _look(loop, page, "特價2️⃣4️⃣5️⃣ 快來買", before, None)

    assert not found and attempt.evidence is None, attempt.steps


def test_without_a_baseline_nothing_can_be_proved(browser_page, monkeypatch, tmp_path):
    """The floor under both comparisons. An empty baseline means we never read the page, and
    against nothing every post that ever existed - last week's included - looks new."""
    loop, page = browser_page
    import channels.facebook_page as fb
    monkeypatch.setattr(fb, "SHOTS_DIR", tmp_path)

    found, attempt = _look(loop, page, "今天新貨到了", {"ids": {}, "text": ""}, WITH_PHOTOS)

    assert not found and attempt.evidence is None, attempt.steps


def test_an_older_post_that_merely_rendered_late_is_not_new(browser_page, monkeypatch, tmp_path):
    """The failure a plain set-difference cannot see. The baseline load rendered two posts and
    this one rendered three; the extra is from last year. Facebook's ids climb with time, so our
    post's id is above everything that was already here - and this one is below."""
    loop, page = browser_page
    before = _baseline(loop, page, monkeypatch, tmp_path)
    assert max(int(i) for i in before["ids"] if i.isdigit()) > 800000000000001

    found, attempt = _look(loop, page, "今天新貨到了", before, OLDER_POST)

    assert not found and attempt.evidence is None, attempt.steps
