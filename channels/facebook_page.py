"""Facebook Page posting: the model drives the browser, code decides whether it worked.

Why an agent and not selectors: the first version of this file was a block of hardcoded
`get_by_role(...)` matchers. It timed out on the real page, which is the expected outcome - and
An earlier project banned that shape at save time, because
and the reason is worth stating:

    browser 的 `[ref=eN]` 是每次 snapshot 後才動態產生 ... browser work can never be decomposed
    into deterministic tool tasks

Why the verification below is NOT part of the agent: the same codebase recorded a `post_fb` node
landing GREEN with `{"post_url": null}` while the runner was offline. The model's `done()` ends the
loop and nothing more; `_verify_published` then reads the page itself, and only that can produce
evidence. The agent cannot reach `success` by saying so.
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from urllib.parse import urlparse

import agent as agent_mod
import llm
import pagetools
import store
from config import PROFILE_DIR, SHOTS_DIR
from store import Post, ValidationError

from browser import open_context
from gate import Attempt

from .base import Channel, ChannelNotReady

SETTING_URL = "fb_page_url"
SETTING_SEEN_LOGIN = "fb_login_seen_at"
SETTING_AUDIENCE = "fb_audience"

# Safe end of the range: a post nobody wanted public is worse than one nobody saw.
DEFAULT_AUDIENCE = "只限本人"
PROFILE = PROFILE_DIR / "facebook"


def resolve_target(raw: str) -> tuple[str, str]:
    """Where to post. Empty means your own timeline, which is a perfectly good destination - it is
    in fact the one with no API at all, so the browser is the only way to reach it.

    A bare name ("mypage") is expanded; a full URL is used as-is. Returns (url, label); the label
    goes into the agent's task so it knows which destination it was told to use.
    """
    raw = (raw or "").strip().rstrip("/")
    if not raw:
        return "https://www.facebook.com/", "你的個人動態"
    if not raw.lower().startswith(("http://", "https://")):
        raw = "https://www.facebook.com/" + raw.lstrip("/")
    path = urlparse(raw).path.strip("/")
    if path in ("", "home.php", "feed"):
        return raw, "你的個人動態"
    # A 社團 is a third kind of destination, not a page. Calling it 粉絲專頁 put a wrong word in
    # the agent's task, which is the one sentence telling it where it is supposed to be.
    if path.lower().startswith("groups/"):
        return raw, f"社團 {path[len('groups/'):].strip('/')}"
    return raw, f"粉絲專頁 {path}"


async def _shot(page, name: str) -> str:
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{name}-{datetime.now():%Y%m%d-%H%M%S}.png"
    await page.screenshot(path=str(SHOTS_DIR / filename))
    return filename


AUDIENCE_LABEL = re.compile(r"分享對象|隱私(設定)?|Audience|Privacy|Who can see")

GROUP_PATH = re.compile(r"^groups/([^/]+)", re.I)


def verify_url(page_url: str, uid: str | None) -> str:
    """Where to go to LOOK for the post afterwards, which is not always where we posted it.

    A group's feed is the wrong place to look, measured twice over: it is sorted 「最相關」 rather
    than by time, so a brand-new post need not be anywhere near the top, and it is full of other
    members' posts, so "something appeared" would not mean we put it there. Facebook has a page
    that lists only one member's posts in one group, in order, and that is where we look.

    A profile or a Page already shows only its own posts, so those verify where they posted.
    """
    hit = GROUP_PATH.match(urlparse(page_url).path.strip("/"))
    if hit and uid:
        return f"https://www.facebook.com/groups/{hit.group(1)}/user/{uid}/"
    return page_url


# What Facebook says back the moment a post is submitted. 「待審核」 is the valuable one: it is the
# difference between "it did not work" and "it worked and an admin has to let it through", and a
# group feed looks identical in both cases.
REPLY_WORDS = re.compile(r"待審核|審核中|等待管理員|管理員審核|已發佈|已發布|發佈失敗|無法發佈|"
                         r"pending approval|awaiting approval|couldn't post|can't post", re.I)


# The chatter every Facebook page load makes, which drowns out whatever the publish itself sent.
RELOAD_CALLS = re.compile(r"Visit|Pagination|Bootstrap|Interstitial|Backups|Prefetch|Nav|"
                          r"Notification|Presence|Typeahead", re.I)


def _watch_graphql(page) -> list[tuple[float, str, int]]:
    """Record every GraphQL call with the time it happened and the status it came back with.

    Reading the page to find out whether a post exists is indirect, and on a 社團 feed it is
    nearly impossible: measured, zero `div[role="article"]`. What is NOT indirect is Facebook's
    own answer to the publish - a POST to /api/graphql whose friendly name says what it did.

    The time matters. The names alone were useless: the first version printed the last few, and
    the last few are always the page reloads that the verification itself had just done.

    This records only. Whether one of these names is a dependable "the post was created" signal
    is a measurement, and it is not made yet - so nothing decides anything on it, it just gets
    reported next to every real publish so the next one answers the question with data.
    """
    seen: list[tuple[float, str, int]] = []

    def note(response) -> None:
        try:
            request = response.request
            if request.method != "POST" or "/graphql" not in request.url:
                return
            body = request.post_data or ""
            status = response.status
        except Exception:
            return
        hit = re.search(r"fb_api_req_friendly_name=([A-Za-z0-9_]+)", body)
        seen.append((time.monotonic(), hit.group(1) if hit else "(unnamed graphql)", status))
        del seen[:-80]

    page.on("response", note)
    return seen


def _calls_at_publish(calls: list[tuple[float, str, int]], fired_at: float) -> str:
    """The calls around the moment publish was pressed, with the page-load chatter removed.

    `publish` ends the agent's turn the instant it clicks, so the create call is in the seconds
    just before `fired_at` - not at the end of the list, where the reloads are.
    """
    window = [(n, s) for t, n, s in calls if fired_at - 30 <= t <= fired_at + 8]
    quiet = [(n, s) for n, s in window if not RELOAD_CALLS.search(n)]
    return "、".join(f"{n}{'' if s == 200 else f' [HTTP {s}]'}"
                     for n, s in (quiet or window)[:8]) or "（沒有看到任何 GraphQL 呼叫）"


def audience_control(snap) -> tuple[dict | None, str | None]:
    """The audience control and the value written into its own name, or (None, None).

    Facebook states the setting inside the control's own name - 「貼文分享對象 只限本人」 - so this
    is readable without opening anything. The whole point is that nothing has to be clicked: the
    menu behind that button is a list of unlabelled radios, and every run that opened it went
    round in circles inside it.

    The control comes back too, because whether it is DISABLED decides what may be done with it.
    """
    for el in snap.elements:
        name = (el.get("name") or "").strip()
        hits = list(AUDIENCE_LABEL.finditer(name))
        if not hits:
            continue
        # The LAST label wins. A group's control is named 「編輯隱私設定。分享對象：成員都看得到」,
        # where matching 隱私設定 first made the value start with 「。分享對象：」 - a value that
        # could never equal anything we might want to set it to.
        value = name[hits[-1].end():].strip(" :：,，、-—。")
        if value:
            return el, value
        # The words with nothing after them are the open menu's own heading, not a setting.
        # Reading that back as the audience would turn "the menu is still open" into a value.
    return None, None


def read_audience(snap) -> str | None:
    """The audience the composer is currently set to, or None if the control is not on screen."""
    return audience_control(snap)[1]


def has_no_audience(snap) -> bool:
    """Is this a composer that genuinely has no audience to choose?

    That is what a Page composer looks like: a Page post is public, so Facebook draws no
    「貼文分享對象」 control at all. Treating that as a refusal would mean 粉絲專頁 could never be
    posted to.

    The editor is the guard. "No audience control" and "we cannot see the page" produce the same
    empty read, and only the first one is a fact about the composer - so unless the text box we
    just typed into is in this snapshot, absence is not evidence of anything.
    """
    return any(el.get("role") == "textbox" for el in snap.elements)


async def _logged_in(ctx) -> bool:
    cookies = await ctx.cookies("https://www.facebook.com")
    return any(c.get("name") == "c_user" and c.get("value") for c in cookies)


class FacebookPage(Channel):
    key = "facebook_page"
    label = "Facebook"
    runtime = "browser"
    hint = ("用一個專門給它用的 Chrome（跟你平常那個分開），登入一次就會一直記得。"
            "發文時它會自己把視窗打開操作，你會看到整個過程。")

    def readiness(self) -> tuple[bool, str]:
        if not llm.load_config().configured:
            return False, "還沒填 API key（FB 只能 agent 驅動）"
        if not store.get_setting(SETTING_SEEN_LOGIN):
            return False, "還沒登入過 Facebook（按「開瀏覽器登入」登一次）"
        return True, f"發到「{resolve_target(store.get_setting(SETTING_URL))[1]}」"

    def check_post(self, post: Post) -> None:
        if not post.body.strip():
            raise ValidationError(
                "請寫一點文字。只有圖片的話，發出去之後沒辦法從畫面上認出哪一則是你的，"
                "就只能跟你說「不確定有沒有成功」。")

    async def open_browser(self, timeout_s: int = 1800) -> dict:
        """Open the dedicated Chrome and LEAVE IT OPEN until the user closes the window.

        The old version closed it the moment it saw a login cookie, which is why the window kept
        vanishing. The window is the only place you can see what this thing is doing - log in,
        look at a post it made, fix a setting - so it closes when you close it, not when some
        condition inside here is satisfied.
        """
        import asyncio

        url, _ = resolve_target(store.get_setting(SETTING_URL))
        seen_login = False
        async with open_context(PROFILE) as ctx:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded")
            deadline = asyncio.get_running_loop().time() + timeout_s
            while asyncio.get_running_loop().time() < deadline:
                if not ctx.pages:                       # the user closed every tab: that is done
                    return {"logged_in": seen_login, "closed": True}
                if not seen_login and await _logged_in(ctx):
                    seen_login = True
                    store.set_setting(SETTING_SEEN_LOGIN, store.now_iso())
                await asyncio.sleep(2)
        return {"logged_in": seen_login, "closed": False}

    # ── publish ──────────────────────────────────────────────────────────────────────────────
    async def publish(self, post: Post, attempt: Attempt, *, dry_run: bool, prefix: str) -> None:
        page_url, where = resolve_target(store.get_setting(SETTING_URL))
        cfg = llm.load_config()
        if not cfg.configured:
            raise ChannelNotReady("還沒設定 LLM（base_url / api_key / model）")

        async with open_context(PROFILE) as ctx:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(page_url, wait_until="domcontentloaded", timeout=60_000)
            attempt.step(f"開啟 {page_url}")

            if not await _logged_in(ctx):
                raise ChannelNotReady("這顆 Chrome profile 沒有 Facebook 登入狀態，請先登入")
            # readiness used to read a flag only the login button wrote, so a profile that was
            # already signed in still showed as "not logged in". This is the real check.
            store.set_setting(SETTING_SEEN_LOGIN, store.now_iso())
            attempt.step(f"確認已登入 · 模型 {cfg.model}")

            # Take the baseline on the page we will check afterwards, which for a 社團 is not the
            # page we post from. Costs one extra load, and without it the comparison is between
            # two different pages and means nothing.
            uid = next((c["value"] for c in await ctx.cookies() if c["name"] == "c_user"), None)
            check_url = verify_url(page_url, uid)
            if check_url != page_url:
                await page.goto(check_url, wait_until="domcontentloaded", timeout=60_000)
            before = await self._before_state(page, attempt)
            if check_url != page_url:
                await page.goto(page_url, wait_until="domcontentloaded", timeout=60_000)
            calls = _watch_graphql(page)

            async def audience_guard() -> None:
                await self._assert_audience(page, attempt)

            operator = agent_mod.BrowserAgent(cfg=cfg, page=page, post=post, attempt=attempt,
                                              dry_run=dry_run, guard=audience_guard)
            claim = ""
            try:
                await operator.run(self._goal(page_url, where, post, dry_run))
            except agent_mod.Stop as stop:
                claim = stop.message
                if not stop.ok:
                    attempt.step(f"agent 沒有完成: {claim[:120]}")
            # `publish` ends the agent's turn the instant it clicks, so this is the moment the
            # post was submitted - which is what tells the recorder which calls to report.
            fired_at = time.monotonic()

            # Whatever the agent claims, the page is the oracle.
            if dry_run:
                # A rehearsal that does not check the audience would tell you nothing about the
                # one setting that decides who sees the real post.
                await self._assert_audience(page, attempt)
                await self._verify_composed(page, post, attempt, prefix)
            else:
                # Read before navigating away: Facebook answers a publish on the spot, and
                # 「待審核」 is the difference between "it failed" and "it is waiting for an admin".
                await self._note_reply(page, attempt)
                await self._verify_published(page, post, attempt, prefix,
                                             check_url, where, before, calls, fired_at, operator)

    async def _assert_audience(self, page, attempt: Attempt) -> None:
        """Make the audience right, then prove it is. Code, not the model.

        Both halves matter. The model could not do this: the options are radios whose words live
        in the row drawn around them, so it was choosing between rows it could not tell apart,
        and it ended on the wrong one with the post already typed. Code can, now that the outline
        carries the row's words and which option is selected.

        The refusal is the floor underneath the fix: if it cannot be made right, nothing is
        published. This runs before `firing()`, so a refusal leaves the attempt provably un-fired.
        """
        want = store.get_setting(SETTING_AUDIENCE) or DEFAULT_AUDIENCE
        # A wider snapshot than the agent's: the control must not be missed just because the
        # composer happened to be busy enough to fill the outline.
        snap = await pagetools.snapshot(page, limit=400)
        control, got = audience_control(snap)
        if got == want:
            attempt.step(f"分享對象是「{want}」")
            return

        # A Page composer has no audience control, because a Page post is public. Said out loud in
        # the steps, because this is also what a renamed control would look like on a profile.
        if got is None and has_no_audience(snap):
            attempt.step("這個發文框沒有分享對象可以選，當成粉絲專頁貼文（一律公開）處理")
            return

        # A 社團 states the audience and then disables the control - members see it, and that is
        # not negotiable. Clicking it anyway spent 15 seconds waiting for a button that is never
        # going to be enabled, and then refused to publish at all.
        if control is not None and control.get("disabled"):
            attempt.step(f"分享對象是「{got}」，Facebook 這裡不讓改，所以跳過這一關")
            return

        if got is not None:
            attempt.step(f"分享對象現在是「{got}」，幫你改成「{want}」")
            try:
                got = await self._set_audience(page, want, attempt)
            except Exception as exc:                     # a failed fix is still a clean refusal
                attempt.step(f"改分享對象時出狀況：{type(exc).__name__}: {exc}")
            if got == want:
                attempt.step(f"分享對象已改成「{want}」")
                return

        if got is None:
            # Not "a Page has no audience" - we could not even find the editor, so we are looking
            # at something that is not a readable composer and cannot tell the two apart.
            raise agent_mod.Refused(
                f"讀不到發文框的內容，分不出這是粉絲專頁還是個人貼文，沒辦法確認誰看得到，"
                f"所以沒有發。（個人貼文需要的是「{want}」）")
        raise agent_mod.Refused(
            f"分享對象是「{got}」，我試著改成「{want}」但改不過去，所以沒有發出去。"
            f"請在 Facebook 上自己改一次，之後它會記住，這個任務再重跑就好。")

    async def _set_audience(self, page, want: str, attempt: Attempt) -> str | None:
        """Open the audience menu, pick `want`, confirm, and report what the control says after.

        Deterministic, but never with a fixed selector: every look is a fresh snapshot, and every
        click targets a ref from that same look. That is the distinction an earlier project's
        `workflow-browser-tool-node-split` lesson draws - refs cannot be decided ahead of time,
        but code that looks and acts in the same breath is fine.
        """
        snap = await pagetools.snapshot(page, limit=400)
        button = next((e for e in snap.elements
                       if AUDIENCE_LABEL.search(e.get("name") or "")), None)
        if button is None:
            return None
        await pagetools.click(page, button["ref"])
        await page.wait_for_timeout(1200)

        expanded = False
        for _ in range(8):
            snap = await pagetools.snapshot(page, limit=400)
            option = next((e for e in snap.elements
                           if (e.get("name") or "").startswith(want)
                           and not AUDIENCE_LABEL.search(e.get("name") or "")), None)
            if option is not None:
                attempt.step(f"選了「{option['name'][:24]}」")
                await pagetools.click(page, option["ref"])
                await page.wait_for_timeout(900)
                break

            # Not on screen: the list scrolls, or the short list hides it behind 「更多選項」.
            moved = (await pagetools.scroll(page, snap.elements[-1]["ref"] if snap.elements
                                            else None, True)).get("moved")
            if moved:
                continue
            more = next((e for e in snap.elements
                         if re.search(r"更多選項|See more|顯示更多", e.get("name") or "")), None)
            if more is None or expanded:
                return read_audience(await pagetools.snapshot(page, limit=400))
            attempt.step("展開完整的分享對象清單")
            await pagetools.click(page, more["ref"])
            await page.wait_for_timeout(1200)
            expanded = True

        # Some of these menus apply immediately; the dialog form needs 「完成」.
        snap = await pagetools.snapshot(page, limit=400)
        confirm = next((e for e in snap.elements
                        if re.fullmatch(r"完成|Done|確定", (e.get("name") or "").strip())), None)
        if confirm is not None:
            await pagetools.click(page, confirm["ref"])
            await page.wait_for_timeout(1200)
        return read_audience(await pagetools.snapshot(page, limit=400))

    def _goal(self, page_url: str, where: str, post: Post, dry_run: bool) -> str:
        """The task, in English - see the prompt convention in AGENTS.md.

        zh-TW survives only as anchors: the on-screen control names the model has to match
        (「貼文分享對象」) and the audience value itself, which is literally the text on the button.
        """
        audience = store.get_setting(SETTING_AUDIENCE) or DEFAULT_AUDIENCE
        lines = [
            f"TASK: publish one post on Facebook, to {where}.",
            f"URL: {page_url}",
            # No preview of the body. It used to show `post.marker` - the body with the emoji
            # stripped out, because that is what the marker is for - and a run read that as the
            # real text, decided the emoji in the editor were extra characters, and retyped the
            # body without them. The model does not need to know the body to call type_body.
            "BODY: type_body puts it in, exactly as the user wrote it. You are not given the "
            "text and you do not need it. Never type it yourself, never 'fix' it, and never "
            "retype it: emoji are drawn as images, so the editor will not look like plain text.",
            f"IMAGES: {len(post.images)}" + (" - attach with upload_images."
                                             if post.images else " - none."),
            "",
            "Route: open the post composer -> type_body"
            + (" -> upload_images" if post.images else "")
            + " -> done. That is the whole job.",
            "",
            "AUDIENCE: not yours to touch. Before anything is published this app reads "
            f"「貼文分享對象」 itself, sets it to 「{audience}」 if it is something else, and refuses "
            "to publish if it cannot. Do NOT open that menu, do NOT click 「更多選項」, and do not "
            "try to verify it - you would only change it by accident. Leave every control alone "
            "except the editor, the photo attachment and the publish button.",
        ]
        if dry_run:
            lines += ["", "DRY RUN: bring the post to the point where it could be published, then "
                          "STOP. Do NOT press publish. Call done and put in `observed` what the "
                          "editor holds."]
        else:
            lines += ["", "Once the editor holds the text"
                          + (" and the images are attached" if post.images else "")
                          + ", use publish to press the publish control ONCE. That is the LAST "
                            "thing you do: pressing it ends your turn. Do not go looking for the "
                            "post afterwards, do not open the profile, do not scroll the feed - "
                            "this app checks the destination itself and reads the whole page, "
                            "which is something you cannot do through a window of the outline."]
        return "\n".join(lines)

    # ── verification: code only, never the model ─────────────────────────────────────────────
    async def _verify_composed(self, page, post: Post, attempt: Attempt, prefix: str) -> None:
        """Dry run: proof is the editor actually holding our text, read back out of the DOM."""
        try:
            snap = await pagetools.snapshot(page)
            needle = post.marker[:12]
            hit = next((e for e in snap.elements
                        if needle in " ".join((e.get("value") or "").split())), None)
            shot = await _shot(page, f"{prefix}-composed")
            if hit:
                attempt.prove("composed_not_published",
                              f"[{hit['ref']}] {hit.get('value', '')[:80]}", shot)
            else:
                attempt.step("預演結束，但編輯器裡找不到我們的文字")
        except Exception as exc:
            attempt.step(f"預演回讀失敗: {type(exc).__name__}: {exc}")

    async def _before_state(self, page, attempt: Attempt) -> dict:
        """What the page we will verify on looked like BEFORE we posted.

        Two string-shaped things, because nothing structural on a Facebook page can be trusted:

          * the post ids carried in its links, which is what proves a post appeared even when it
            has no readable text at all;
          * the page's whole text, which is what proves it when the ids cannot be read.

        The baseline is also what makes a short marker usable: 「快來買」 is on last week's post
        too, so alone it proves nothing - but it is in the BEFORE text as well, so it is not new.
        """
        state = {"ids": {}, "text": ""}
        try:
            # Wait first. A baseline taken off a page that has not finished rendering is worse
            # than no baseline at all, because afterwards everything looks new.
            await page.wait_for_timeout(5000)
            state["ids"] = await pagetools.post_ids(page)
            state["text"] = await pagetools.page_text(page)
        except Exception as exc:
            attempt.step(f"記發文前的樣子時出狀況：{type(exc).__name__}: {exc}")
        attempt.step(f"記下發文前的樣子，等發完回來比對（{len(state['ids'])} 則貼文、"
                     f"{len(state['text'])} 字）")
        return state

    async def _note_reply(self, page, attempt: Attempt) -> None:
        """What the page said in answer to the publish, before we navigate away from it."""
        try:
            text = await pagetools.page_text(page)
        except Exception:
            return
        hit = REPLY_WORDS.search(text)
        if hit:
            attempt.step(f"Facebook 回了一句：…{text[max(0, hit.start() - 20):hit.end() + 40]}…")

    async def _verify_published(self, page, post: Post, attempt: Attempt, prefix: str,
                                check_url: str, where: str,
                                before: dict | None = None,
                                calls: list | None = None,
                                fired_at: float = 0.0,
                                operator=None) -> None:
        """Real run: go to the destination, load it fresh, and find the post we just made.

        Reloading whatever page the composer left behind is not proof. The composer redirects to
        /post/create, and a feed that already had the text on screen would "confirm" a post that
        was never made. So this NAVIGATES to the destination - your timeline or the page you
        chose - loads it from the server, reads it, and screenshots what it read.

        What identifies the post is a permalink at the top that was NOT there before we posted.
        Every other signal is a guess at what our own post must look like, and each guess has
        been wrong: Facebook draws emoji as <img>, so an emoji-heavy body leaves nothing to search
        for (「gegege3️⃣4️⃣5️⃣6️⃣7️⃣」 leaves "gegege"), and a timestamp has to be parsed in whatever
        words and locale Facebook felt like using. A permalink that appeared is none of that - it
        is Facebook's own id for a post that did not exist a minute ago.

        Never raises. No evidence means `uncertain`, which is the honest answer to "we pressed
        publish and then could not confirm".
        """
        needle = post.marker[:20]
        for look in (1, 2, 3):
            try:
                await page.goto(check_url, wait_until="domcontentloaded", timeout=60_000)
                await page.wait_for_timeout(4000 if look == 1 else 7000)
                if await self._found_it(page, post, attempt, prefix, where, needle,
                                        before or {}):
                    return
                attempt.step(f"第 {look} 次到{where}重新整理：還沒看到這則貼文")
            except Exception as exc:
                attempt.step(f"回讀失敗: {type(exc).__name__}: {exc}")
                return

            # Second pass: hand what we found back to the SAME agent, in the same conversation,
            # and let it look. It cannot declare success - only the text match above can do that
            # - but it can see a dialog still open, an upload still spinning, or a block notice.
            if look == 2 and operator is not None:
                try:
                    said = await operator.resume(
                        f"The post is NOT on {where} after two reloads. You are looking at it now."
                        " Find out why: is a composer or dialog still open, is an upload still in"
                        " progress, is there an error or a block notice, or did the publish never"
                        " land? If something still needs pressing to finish publishing, press it."
                        " Then call done and describe exactly what you saw.")
                    attempt.step(f"回去看了一眼：{said[:120]}")
                except agent_mod.Stop as stop:
                    attempt.step(f"回去看了一眼：{stop.message[:120]}")
                except Exception as exc:
                    attempt.step(f"回去看的時候出狀況：{type(exc).__name__}: {exc}")
        shot = await _shot(page, f"{prefix}-not-found")
        attempt.step(f"最後還是沒在{where}上看到，截圖留著（{shot}）")
        # Only on failure, and only as a lead: whether one of these is reliably "the post was
        # created" is a question for the next run that fails, not something to guess at now.
        if calls:
            attempt.step("按下發佈當下的網路請求：" + _calls_at_publish(calls, fired_at))

    async def _found_it(self, page, post: Post, attempt: Attempt, prefix: str, where: str,
                        needle: str, before: dict) -> bool:
        """Did something appear here that was not here before we published?

        Two comparisons, both on strings, because the DOM around a Facebook post is deliberately
        unreadable - measured on this very group: no `role="article"` at all, obfuscated class
        names, a timestamp split across decoy spans. Everything that parsed post blocks or read
        「剛剛」 off them has been deleted; it never once worked outside its own fixtures.

        Neither comparison means anything without a baseline, which is why an empty `before` can
        prove nothing. Empty means "we never managed to read this page", not "this page was
        empty", and against nothing every post that has ever existed looks new.
        """
        was_ids, was_text = before.get("ids") or {}, before.get("text") or ""

        # A post id Facebook did not have here before. Four photos of one post all carry the same
        # `set=pcb.<id>`, so this works with no readable text at all.
        now_ids = await pagetools.post_ids(page)
        new = [(i, href) for i, href in now_ids.items() if i not in was_ids]
        # Facebook's ids climb with time - measured, the 21:57 post is 900000000000001 and the
        # 22:07 one is 900000000000002 - so our post's id is above every id that was already
        # here. That is what separates "a new post" from "an older post this load happened to
        # render and the baseline load did not", which is otherwise the same thing to a diff.
        ceiling = max((int(i) for i in was_ids if i.isdigit()), default=None)
        if ceiling is not None:
            new = [(i, href) for i, href in new if not i.isdigit() or int(i) > ceiling]
        if was_ids and new:
            shot = await _shot(page, f"{prefix}-published")
            attempt.step(f"{where}上多了一則發文前沒有的貼文，截圖存檔")
            attempt.prove("post_url", new[0][1], shot)
            return True

        # Words that were not here before. The baseline is what makes a SHORT marker usable:
        # 「快來買」 proves nothing by itself because it is on every post the shop ever made - but
        # it is in the before text too, so it is not new, while 「gegege」 is not and was not.
        now_text = await pagetools.page_text(page)
        if needle and was_text and needle not in was_text and needle in now_text:
            shot = await _shot(page, f"{prefix}-published")
            attempt.step(f"{where}上出現了發文前沒有的內容「{needle[:20]}」，截圖存檔")
            attempt.prove("feed_text_new", needle, shot)
            return True
        return False
