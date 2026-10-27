"""Vision fallback for LINE: when a template stops matching, ask the model where the thing is.

LINE is Qt/QML with an empty accessibility tree, so there is no outline to read - the only
observation channel is pixels. Template matching on the window's own bitmap is the fast path:
precise, free, and position-independent. This is what happens when it misses, usually because LINE
moved something in an update.

**Vision may navigate. Vision may not fire.** A VLM's pixel coordinates are good enough to find a
tab or a menu entry and wrong often enough that aiming them at an irreversible publish button is a
bet this project does not take. `line_note.py` refuses the fallback on the fire step, so a missing
publish template is a `failed` run - provably nothing posted - rather than a click somewhere
unknown.

A successful rescue re-saves the template at the new position, so the fast path heals itself.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import llm

PROMPT = """This is a screenshot of the LINE desktop app, {w} pixels wide and {h} pixels tall.

Find: {what}

Answer with ONLY a JSON object, no prose, no code fence:
{{"found": true/false, "x": <int>, "y": <int>, "why": "<short reason in zh-TW>"}}

x,y must be the CENTRE of the thing, in pixels from the TOP-LEFT CORNER of this image.
If you cannot see it clearly, answer {{"found": false, "x": 0, "y": 0, "why": "..."}} -
a wrong guess is worse than saying no."""


def _parse(text: str) -> dict | None:
    text = llm.strip_think(text)      # a reasoning model's scratchpad can contain braces
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


async def locate(cfg: llm.LLMConfig, shot_path: Path, size: tuple[int, int],
                 what: str) -> tuple[int, int, str] | None:
    """Ask the vision model for the centre of `what`. Returns (x, y, why) or None."""
    width, height = size
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": PROMPT.format(w=width, h=height, what=what)},
            llm.image_part(shot_path),
        ],
    }
    reply = await llm.chat(cfg, [message], model=cfg.vision_model, max_tokens=900)
    data = _parse(reply.get("content") or "")
    if not data or not data.get("found"):
        return None

    try:
        x, y = int(data["x"]), int(data["y"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 <= x < width and 0 <= y < height):
        return None
    return x, y, str(data.get("why") or "")[:80]


def save_template(image, centre: tuple[int, int], out_path: Path,
                  size: tuple[int, int] = (120, 40)) -> None:
    """Crop around the rescued point and keep it, so the fast path works next time."""
    x, y = centre
    half_w, half_h = size[0] // 2, size[1] // 2
    box = (max(0, x - half_w), max(0, y - half_h),
           min(image.width, x + half_w), min(image.height, y + half_h))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.crop(box).save(out_path)


TRANSCRIBE = """Read this screenshot of the LINE desktop app and transcribe the text you can
actually see in it. Plain text only, no commentary, no translation, no guessing at anything that is
cut off or illegible. If a piece of text is unclear, leave it out rather than inventing it."""


async def transcribe(cfg: 'llm.LLMConfig', shot_path: Path, size: tuple[int, int]) -> str:
    """What words are on this screen.

    Deliberately a reading task, not a judgement. The caller compares the transcript against the
    text it tried to post and decides for itself whether the post is there - so the model never
    gets to answer "did it work?", which is the question it can get wrong in the direction that
    costs you a post you think went out and did not.
    """
    message = {"role": "user", "content": [
        {"type": "text", "text": TRANSCRIBE},
        llm.image_part(shot_path),
    ]}
    reply = await llm.chat(cfg, [message], model=cfg.vision_model, max_tokens=1500)
    return llm.strip_think(reply.get("content") or "")


DECIDE = """You drive the LINE desktop app for ONE job: putting a post into a group's
記事本 (Note).

YOU ARE LOOKING AT the window titled 「{window_title}」, {w}x{h} pixels. All coordinates you give
are inside THIS window.

WHAT LINE LOOKS LIKE (this machine, measured - trust this over anything you remember)
- Far left: a narrow dark rail of round icons (profile, chats, friends, ...). Rarely needed.
- Top of the list pane: the tabs 全部 / 好友 / 群組 / 社群 / 官方帳號, around y=25.
- Under the tabs: the search box, placeholder 「搜尋聊天和訊息」, around y=82.
- Below that: the chat list. Each row is an avatar, a name, and a preview line.
- Bottom of the list pane: an advertisement strip. NEVER click it.
- Right pane, once a chat is open: the group name top-left, and at the TOP-RIGHT a row of icons
  at about y=81 — 🔍 search (~x=635), 📞 call (~x=664), 📄 **記事本** (~x=693), ⋮ more (~x=720).
  **記事本 is that document icon in the toolbar. It is NOT inside a dropdown menu.**
- A group may also show a pinned announcement bar under the title with a ∨ on its right.
  That ∨ only expands the announcement. It is NOT a menu and NEVER leads to 記事本.
- Bottom right: the message input box 「輸入訊息」. That is the CHAT box, not the Note editor.
  Typing the post there would send it as a chat message - wrong.

THE 記事本 WINDOW (this is where the post actually gets written)
- Clicking the 記事本 icon opens a SEPARATE, NARROW window, roughly 330x590, whose title is the
  GROUP'S NAME rather than "LINE". When that happens the screenshot you are given switches to
  that window and its coordinates - the main window is no longer what you are looking at.
- Inside it: tabs 相簿 / 記事本 near the top (make sure 記事本 is the selected one), then a field
  with the placeholder 「新增記事本」, then the existing notes below.
- To write a post: click 「新增記事本」, then type_body, then the photo control if there are
  images, then the button that confirms the post.

THE JOB
{goal}

THE TEXT THAT WILL BE POSTED (you never type it yourself - call type_body and it is inserted
verbatim, including capitals, punctuation and emoji):
{preview}

WHAT YOU HAVE DONE SO FAR
{history}

Reply with ONLY a JSON object, nothing before or after it, no code fence:

  {{"action":"click","x":<int>,"y":<int>,"what":"<在點什麼，zh-TW>","why":"<短理由>"}}
  {{"action":"type_group"}}    the group name goes into the focused search box
  {{"action":"type_body"}}     the post text goes into the focused Note editor, verbatim
  {{"action":"attach"}}        hand the images to the file dialog your last click opened
  {{"action":"publish","x":<int>,"y":<int>,"what":"<發布按鈕>"}}
  {{"action":"done","observed":"<畫面上看到什麼表示完成了>"}}
  {{"action":"give_up","why":"<你看到什麼，zh-TW>"}}

RULES
- Keep your reasoning SHORT. The JSON matters; an unfinished thought is a wasted turn.
- x,y are the CENTRE of the target, in pixels from the TOP-LEFT of the image.
- The usual route is: 群組 tab → search box → type_group → click the group row → the 記事本 icon
  in the top-right toolbar → the button that starts a new note (＋ or 「建立貼文」) → click into
  the note's text area → type_body → (photo icon → attach) → publish.
  Check the screen each turn: if you are already past a step, skip it.
- `publish` is ONLY the final button that actually posts the note. Everything else is `click`.
- If your last click changed nothing, the history will say so. Pick a DIFFERENT target - never
  repeat a click that did nothing.
- Never invent a coordinate for something you cannot see. Say give_up and describe the screen."""


async def decide(cfg: "llm.LLMConfig", shot_path: Path, size: tuple[int, int],
                 goal: str, history: list[str], preview: str = "",
                 window_title: str = "LINE") -> dict | None:
    """One look, one decision. The model picks the route; the caller enforces the limits.

    The budget is large because this model reasons at length before answering, and a reply cut off
    inside `<think>` leaves nothing to parse - which is what "決定不出下一步" actually was. One
    retry with a blunter instruction covers the rest.
    """
    done = "\n".join(f"- {h}" for h in history[-12:]) or "- （還沒做任何事）"
    text = DECIDE.format(w=size[0], h=size[1], goal=goal, history=done,
                         window_title=window_title,
                         preview=preview or "（這次沒有要輸入的文字）")
    message = {"role": "user", "content": [
        {"type": "text", "text": text},
        llm.image_part(shot_path),
    ]}

    for nudge in ("", "\n\n上一次回覆沒有給出可用的 JSON。這次直接輸出 JSON，不要任何說明。"):
        msgs = [message] if not nudge else [
            {"role": "user", "content": [{"type": "text", "text": text + nudge},
                                         llm.image_part(shot_path)]}]
        reply = await llm.chat(cfg, msgs, model=cfg.vision_model, max_tokens=4000)
        data = _parse(reply.get("content") or "")
        if isinstance(data, dict) and data.get("action"):
            return data
    return None
