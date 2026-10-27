"""What the code still decides for itself once the model picks the route.

The click path is no longer written down anywhere — it was, and it was wrong on the real app
(it assumed 記事本 lived in a dropdown; it is an icon in the chat toolbar). So the model looks and
decides. These are the limits that survive that.
"""
import asyncio
from types import SimpleNamespace

import pytest

from channels import line_note
from channels.base import ChannelNotReady
from gate import Attempt


@pytest.fixture()
def channel(monkeypatch, tmp_path):
    monkeypatch.setattr(line_note, "LINE_TEMPLATES", tmp_path)
    monkeypatch.setattr(line_note, "SHOTS_DIR", tmp_path)
    return line_note.LineGroupNote()


def _shot():
    return SimpleNamespace(save=lambda *_a, **_k: None, width=742, height=506)


# ── the one irreversible click ───────────────────────────────────────────────────────────────
def test_a_real_run_will_not_press_publish_on_a_coordinate_it_was_told(channel, monkeypatch):
    """The model may FIND the button. It never supplies the coordinate that gets clicked for real
    — that comes from a saved picture, matched pixel for pixel."""
    clicked = []
    monkeypatch.setattr(line_note.linewin, "click_once", lambda *a, **k: clicked.append(a))
    monkeypatch.setattr(line_note.linewin, "click", lambda *a, **k: clicked.append(a))

    with pytest.raises(ChannelNotReady) as exc:
        channel._publish(None, {"action": "publish", "x": 500, "y": 400}, Attempt("line"),
                         dry_run=False, prefix="t", shot=_shot())

    assert "預演" in str(exc.value)
    assert not clicked


def test_a_dry_run_caches_the_button_and_does_not_press_it(channel, monkeypatch, tmp_path):
    saved = []
    clicked = []
    monkeypatch.setattr(line_note.vision, "save_template",
                        lambda img, centre, path: saved.append((centre, path)))
    monkeypatch.setattr(line_note.linewin, "click_once", lambda *a, **k: clicked.append(a))
    monkeypatch.setattr(line_note.linewin, "click", lambda *a, **k: clicked.append(a))
    monkeypatch.setattr(line_note, "_save", lambda img, name: "shot.png")

    attempt = Attempt("line")
    channel._publish(None, {"action": "publish", "x": 500, "y": 400}, attempt,
                     dry_run=True, prefix="t", shot=_shot())

    assert saved and saved[0][0] == (500, 400), "the dry run is what fills the cache"
    assert not clicked, "a dry run must never press publish"
    assert attempt.evidence is not None
    assert attempt.stage.value == "not_fired"


def test_publishing_for_real_matches_the_cached_picture(channel, monkeypatch, tmp_path):
    (tmp_path / "publish.png").write_bytes(b"cached")
    monkeypatch.setattr(line_note.linewin, "locate", lambda shot, path: (610, 470, 0.97))
    clicked, repeated = [], []
    monkeypatch.setattr(line_note.linewin, "click_once",
                        lambda win, x, y, **k: clicked.append((x, y)))
    # the retrying primitive must not be reachable from the publish path: it presses twice
    monkeypatch.setattr(line_note.linewin, "click", lambda *a, **k: repeated.append(a))

    attempt = Attempt("line")
    channel._publish(None, {"action": "publish", "x": 1, "y": 1}, attempt,
                     dry_run=False, prefix="t", shot=_shot())

    assert clicked == [(610, 470)], "the cached match wins over whatever the model said"
    assert not repeated, "publish must never use the press-twice primitive"
    assert attempt.stage.value == "unknown", "pressing publish must mark the attempt as fired"


# ── verification is a reading task, not a verdict ────────────────────────────────────────────
def test_verification_reads_the_screen_instead_of_asking_if_it_worked(channel, monkeypatch):
    """A model that can answer "did it post?" is a model that can answer yes when it did not."""
    monkeypatch.setattr(line_note.linewin, "capture", lambda win: _shot())
    monkeypatch.setattr(line_note.time, "sleep", lambda s: None)
    monkeypatch.setattr(line_note.time, "time", lambda _c=iter([0, 1, 100]): next(_c))

    # _verify now waits through line_note._await so that a stop lands during a model call;
    # the test replaces that wait rather than the raw future.
    monkeypatch.setattr(line_note, "_await",
                        lambda coro, loop, attempt, timeout=0: "完全不相干的畫面文字")

    attempt = Attempt("line")
    channel._verify(SimpleNamespace(width=10, height=10),
                    SimpleNamespace(body="今天開賣 🎉"), attempt, None, None, "t")

    assert attempt.evidence is None, "text that is not ours is not evidence"


# ── the loop cannot run away ─────────────────────────────────────────────────────────────────
def test_the_look_loop_is_bounded():
    assert 1 < line_note.MAX_TURNS <= 40


# ── the model-facing helpers must actually run ───────────────────────────────────────────────
# An import succeeds even when a function body references a name that does not exist there; the
# NameError only lands when that line runs, which was mid-publish. These execute the real bodies
# with only the network call stubbed.
_CFG = SimpleNamespace(vision_model="test-model", model="test-model",
                       base_url="http://x", api_key="k", configured=True)


def _png(tmp_path):
    from PIL import Image

    path = tmp_path / "shot.png"
    Image.new("RGB", (40, 30), (240, 240, 240)).save(path)
    return path


def _stub_chat(monkeypatch, content):
    async def fake(cfg, messages, **kw):
        assert messages[0]["content"][1]["type"] == "image_url", "the screenshot must be attached"
        return {"content": content}

    monkeypatch.setattr(line_note.vision.llm, "chat", fake)
    return asyncio


def test_decide_runs_end_to_end(monkeypatch, tmp_path):
    from channels.line_note import vision

    asyncio = _stub_chat(monkeypatch, '<think>想一下</think>{"action":"click","x":5,"y":6,'
                                      '"what":"記事本","why":"在工具列上"}')
    out = asyncio.run(vision.decide(_CFG, _png(tmp_path), (40, 30), "goal", ["做過一件事"]))
    assert out["action"] == "click" and out["x"] == 5


def test_transcribe_runs_end_to_end(monkeypatch, tmp_path):
    from channels.line_note import vision

    asyncio = _stub_chat(monkeypatch, "<think>讀一下</think>畫面上的文字")
    out = asyncio.run(vision.transcribe(_CFG, _png(tmp_path), (40, 30)))
    assert out == "畫面上的文字", "the scratchpad must not reach the caller"


def test_locate_runs_end_to_end(monkeypatch, tmp_path):
    from channels.line_note import vision

    asyncio = _stub_chat(monkeypatch, '{"found":true,"x":12,"y":9,"why":"看到了"}')
    out = asyncio.run(vision.locate(_CFG, _png(tmp_path), (40, 30), "記事本"))
    assert out[:2] == (12, 9)
