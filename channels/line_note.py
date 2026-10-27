"""LINE group Note (群組記事本), driven through the desktop app.

There is no other way in, and the obvious alternatives all fail for different reasons: the Chrome
extension "web LINE" never had Notes at all (and is being retired), the Messaging API can push and
broadcast but cannot write a Note, and an Android emulator or a second Windows VM would sign your
real device out, because LINE allows one phone plus one PC per account. So it is this machine's
already-signed-in LINE. `linewin.py` has how that is done without taking the cursor or keyboard.

**Nobody teaches it anything.** The sequence of clicks is always the same, so it lives here as a
constant. What differs between machines is only what the buttons LOOK like, and the model works
that out by looking at the screen the first time. Each thing it finds is cropped and cached, so the
second run matches the picture directly - fast, free and exact - and only looks again when LINE
moves something.

Two limits stay, and they are why this is not simply "let the model click things":

  * **The publish button must come from a cached picture, never from a guess.** A dry run locates
    it and caches it WITHOUT clicking, so the first real run already has it. With no cache, a real
    run fails rather than aiming a guessed coordinate at an irreversible button.
  * **The model is never asked whether it worked.** It is asked to transcribe what is on screen;
    the code decides whether the post text is in that transcript. A model that can answer "did it
    post?" is a model that can answer yes when it did not - a real incident elsewhere
    (`test_browser_false_success_gate.py`), and the reason the gate sits outside the model.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import linewin
import llm
import store
import vision
from config import LINE_TEMPLATES, SHOTS_DIR
from store import Post, ValidationError

from gate import Attempt

from .base import Channel, ChannelNotReady

SETTING_GROUP = "line_group_name"


PUBLISH_TEMPLATE = "publish"       # the one thing that is cached rather than re-judged
MAX_TURNS = 18                     # a wrong route must cost a bounded number of looks


def template_path(name: str) -> Path:
    return LINE_TEMPLATES / f"{name}.png"


def learned() -> list[str]:
    return sorted(p.stem for p in LINE_TEMPLATES.glob("*.png"))


def _save(image, name: str) -> str:
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{name}-{datetime.now():%Y%m%d-%H%M%S}.png"
    image.save(SHOTS_DIR / filename)
    return filename


def _await(coro, loop, attempt: Attempt, timeout: float = 200.0):
    """Wait for a coroutine on the main loop, checking for a stop while we wait.

    A plain `.result(timeout=...)` blocks this worker thread for as long as the model takes, so a
    stop pressed during a 40-second vision call would sit unanswered until the call returned.
    Polling in one-second slices makes 終止 land in about a second without ever interrupting an
    action half-way.
    """
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    deadline = time.time() + timeout
    while True:
        try:
            return future.result(timeout=1.0)
        except concurrent.futures.TimeoutError:
            if attempt.stop_requested:
                future.cancel()
                attempt.check_stop()
            if time.time() > deadline:
                future.cancel()
                raise TimeoutError("等模型回覆超時")


class LineGroupNote(Channel):
    key = "line_group_note"
    label = "LINE 群組記事本"
    runtime = "desktop"
    hint = ("用你這台已經登入的 LINE，它會自己看畫面決定要點哪裡。"
            "LINE 視窗要開著 —— 可以被其他視窗蓋住，但不能縮到工作列。"
            "⚠ 目前只能發文字，圖片會被略過（LINE 的照片按鈕不吃程式送的點擊）。")

    def readiness(self) -> tuple[bool, str]:
        group = store.get_setting(SETTING_GROUP)
        if not group:
            return False, "還沒填要發到哪個群組"
        if not llm.load_config().configured:
            return False, "還沒填 API key"
        try:
            linewin.find_window()
        except linewin.LineNotAvailable as exc:
            return False, str(exc)
        if not template_path(PUBLISH_TEMPLATE).exists():
            return False, f"可以發到「{group}」，但第一次請先跑預演，讓它認得發布鍵"
        return True, f"發到「{group}」"

    def check_post(self, post: Post) -> None:
        if not post.body.strip():
            raise ValidationError("記事本貼文要有內文")

    async def publish(self, post: Post, attempt: Attempt, *, dry_run: bool, prefix: str) -> None:
        # linewin blocks (it sleeps between messages and waits for redraws), so the body runs off
        # the event loop. The model calls are async and reach back to this loop from that thread.
        loop = asyncio.get_running_loop()
        cfg = llm.load_config()
        if not cfg.configured:
            raise ChannelNotReady("還沒填 API key")
        group = store.get_setting(SETTING_GROUP)
        if not group:
            raise ChannelNotReady("還沒填要發到哪個群組")
        await asyncio.to_thread(self._run, post, attempt, dry_run, prefix, cfg, loop, group)

    # ── look → decide → act ──────────────────────────────────────────────────────────────────
    def _run(self, post: Post, attempt: Attempt, dry_run: bool, prefix: str,
             cfg: llm.LLMConfig, loop, group: str) -> None:
        win = linewin.active_window()
        attempt.step(f"LINE 視窗「{win.title}」{win.width}×{win.height}")

        goal = (f"Find the group 「{group}」 in LINE, open its 「記事本」 (Note), start a new "
                "post, type the body in"
                + (f", attach {len(post.images)} image(s)" if post.images else "")
                + ", and publish it.")
        history: list[str] = []
        last_click: tuple[int, int] | None = None
        repeated = 0

        for turn in range(1, MAX_TURNS + 1):
            attempt.check_stop()
            # Re-read every turn: 記事本 opens in its OWN window, and the click that opens it
            # leaves the main window unchanged. Without this the run goes blind at exactly the
            # moment it succeeds.
            current = linewin.active_window()
            if current.hwnd != win.hwnd:
                attempt.step(f"→ 畫面換到「{current.title}」視窗 "
                             f"{current.width}×{current.height}")
                history.append(f"(the screen moved to the 「{current.title}」 window)")
                win = current
            before = linewin.capture(win)
            SHOTS_DIR.mkdir(parents=True, exist_ok=True)
            path = SHOTS_DIR / f"{prefix}-look-{turn:02d}.png"
            before.save(path)

            act = _await(
                vision.decide(cfg, path, (win.width, win.height), goal, history,
                              preview=post.body[:160], window_title=win.title),
                loop, attempt)
            if not act:
                raise linewin.LineNotAvailable("看了畫面但決定不出下一步")

            action = act.get("action")
            what = str(act.get("what") or "").strip()
            why = str(act.get("why") or "").strip()

            if action == "give_up":
                raise linewin.LineNotAvailable(str(act.get("why") or "它說它卡住了"))

            if action == "done":
                attempt.step(f"它認為做完了：{str(act.get('observed'))[:100]}")
                break

            if action == "type_group":
                linewin.type_text(win, group)
                attempt.step(f"[{turn}] 輸入群組名稱「{group}」")
                history.append(f"typed 「{group}」 into the search box")
                continue

            if action == "type_body":
                linewin.type_text(win, post.body)
                attempt.step(f"[{turn}] 輸入內文（{len(post.body)} 字，含大小寫與表情符號）")
                history.append("typed the post body into the editor")
                continue

            if action == "attach":
                self._attach(win, post, attempt, turn)
                history.append("handed the images to the file dialog")
                continue

            if action == "publish":
                self._publish(win, act, attempt, dry_run, prefix, before)  # front window
                if dry_run:
                    return
                history.append("pressed publish")
                continue

            if action != "click":
                history.append(f"({action} is not an available action)")
                continue

            try:
                x, y = int(act["x"]), int(act["y"])
            except (KeyError, TypeError, ValueError):
                history.append("(the coordinates you gave could not be read)")
                continue

            if last_click == (x, y):
                repeated += 1
                if repeated >= 2:
                    raise linewin.LineNotAvailable(
                        f"一直重複點同一個地方（{what or f'{x},{y}'}）但畫面沒有反應，停下來")
            else:
                repeated = 0
            last_click = (x, y)

            linewin.click(win, x, y)
            moved = linewin.region_changed(before, linewin.capture(win),
                                           (0, 0, win.width, win.height))
            attempt.step(f"[{turn}] 點「{what or f'({x},{y})'}」"
                         + (f" — {why}" if why else "")
                         + ("" if moved > 400 else "（畫面沒什麼變化）"))
            history.append(f"clicked 「{what}」 @({x},{y})"
                           + ("" if moved > 400 else " - the screen did not change, so that was "
                              "probably the wrong spot"))
        else:
            raise linewin.LineNotAvailable(f"看了 {MAX_TURNS} 次還沒完成，停下來")

        if not dry_run:
            self._verify(linewin.active_window(), post, attempt, cfg, loop, prefix)

    def _publish(self, win, act: dict, attempt: Attempt, dry_run: bool, prefix: str,
                 shot) -> None:
        """The one click that cannot be taken back.

        A dry run CACHES the button it found and stops without pressing it, so the first real run
        has a picture to match. A real run matches that picture - a saved crop, compared pixel for
        pixel - and refuses to run at all without one. The model may find the button; it never
        supplies the coordinate that gets clicked for real.
        """
        path = template_path(PUBLISH_TEMPLATE)
        if dry_run:
            try:
                x, y = int(act["x"]), int(act["y"])
            except (KeyError, TypeError, ValueError):
                attempt.step("預演：它說要發布了，但沒給得出發布鍵的位置")
                return
            vision.save_template(shot, (x, y), path)
            attempt.step(f"[預演] 找到發布鍵 @({x},{y})，記起來了 —— 沒有按下去")
            attempt.prove("composed_not_published", f"已認得發布鍵 @({x},{y})",
                          _save(shot, f"{prefix}-dryrun"))
            return

        if not path.exists():
            raise ChannelNotReady(
                "還沒認得發布鍵 —— 先跑一次預演就會了。"
                "（不拿看來的座標去按發布，那一下收不回來。）")
        x, y, score = linewin.locate(shot, path)
        attempt.firing(f"發布 @({x},{y})（比對記住的樣子，{score:.2f}）")
        # click_once, never click: a press that cannot be confirmed is a run the gate settles as
        # `uncertain` for a human to check. A second press is a second post.
        linewin.click_once(win, x, y, settle=2.2)

    def _attach(self, win, post: Post, attempt: Attempt, i: int) -> None:
        """Images do not currently go into a LINE note. Measured, not assumed:

          * The ＋ in the note's photo area does not react to injected clicks - no window opens,
            nothing redraws, even after waiting ten seconds for a packaged-app file picker.
          * Pasting does not work either: the image goes onto the clipboard fine, but Qt reads
            modifier state from the OS, so a posted `WM_KEYDOWN VK_CONTROL` is not a held Ctrl and
            Ctrl+V does nothing.

        The route that would work is `SendInput`, which injects into the system input queue - real
        keyboard, real focus, the user's machine taken over for the duration. That is the one thing
        this channel exists to avoid, so the post goes out as text and says so rather than silently
        dropping the pictures.
        """
        if not post.images:
            attempt.step(f"[{i}] 這則沒有圖片")
            return
        attempt.step(f"[{i}] ⚠ LINE 記事本目前無法自動帶圖片，這則只發文字"
                     f"（{len(post.images)} 張圖被略過）")
        return
        paths = " ".join(f'"{p}"' for p in post.image_paths())
        # The operator clicks the photo control itself and then calls `attach`; there is no
        # template to check for any more. The leftover check here always failed, so images were
        # silently dropped and the post went out as text only.
        edit = _wait_for_file_dialog_edit(timeout=12.0)
        if edit is None:
            raise ChannelNotReady(
                "等不到 Windows 選檔視窗 —— 上一步可能沒點到記事本編輯器裡的照片按鈕")
        linewin.user32.SendMessageW(edit, 0x000C, 0, paths)          # WM_SETTEXT
        linewin.user32.PostMessageW(edit, linewin.WM_KEYDOWN, linewin.VK_RETURN, 1)
        linewin.user32.PostMessageW(edit, linewin.WM_KEYUP, linewin.VK_RETURN, 1)
        time.sleep(3.0)
        attempt.step(f"[{i}] 帶入 {len(post.images)} 張圖片：{[p.name for p in post.image_paths()]}")

    # ── read the result back ─────────────────────────────────────────────────────────────────
    def _verify(self, win, post: Post, attempt: Attempt, cfg, loop, prefix: str) -> None:
        """Ask the model to TRANSCRIBE the screen, then check the transcript ourselves.

        The distinction is the whole point. "Did it post?" is a question a model can get wrong in
        the expensive direction. "What words are on this screen?" is a reading task, and whether
        our post is among those words is then a string comparison the model has no vote in.

        Never raises: no match means no evidence, which the gate settles as `uncertain` - the
        honest answer to "we pressed publish and could not confirm".
        """
        deadline = time.time() + 30
        needle = " ".join(post.body.split())[:18]
        while time.time() < deadline:
            shot = linewin.capture(win)
            SHOTS_DIR.mkdir(parents=True, exist_ok=True)
            path = SHOTS_DIR / f"verify-{datetime.now():%H%M%S}.png"
            shot.save(path)
            try:
                seen = _await(vision.transcribe(cfg, path, (win.width, win.height)),
                              loop, attempt)
            except Exception as exc:
                attempt.step(f"讀畫面失敗: {type(exc).__name__}")
                return
            if needle and needle in " ".join((seen or "").split()):
                attempt.prove("note_text_on_screen", needle, _save(shot, f"{prefix}-published"))
                return
            time.sleep(3)
        attempt.step("發布後在畫面上找不到這則貼文的文字 —— 可能已經發出去了，請自己確認")


def _wait_for_file_dialog_edit(timeout: float) -> int | None:
    """The filename Edit inside the standard open dialog (class #32770), whoever owns it."""
    import ctypes
    from ctypes import wintypes

    user32 = linewin.user32
    deadline = time.time() + timeout
    while time.time() < deadline:
        dialogs: list[int] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def top(hwnd, _l):
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            if cls.value == "#32770" and user32.IsWindowVisible(hwnd):
                dialogs.append(hwnd)
            return True

        user32.EnumWindows(top, 0)
        for dlg in dialogs:
            edits: list[int] = []

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def child(hwnd, _l):
                cls = ctypes.create_unicode_buffer(64)
                user32.GetClassNameW(hwnd, cls, 64)
                if cls.value == "Edit":
                    edits.append(hwnd)
                return True

            user32.EnumChildWindows(dlg, child, 0)
            if edits:
                return edits[0]
        time.sleep(0.3)
    return None
