"""The durable rules: the claim is a real CAS, the reaper is safe for a publisher, and bad content
is refused at the write boundary rather than at 9am on a schedule."""
from datetime import datetime, timedelta

import pytest


def soon() -> str:
    return (datetime.now() + timedelta(hours=1)).replace(microsecond=0).isoformat()


def test_claim_is_a_real_cas(db):
    post = db.create_post("t", "body", [])
    sid = db.create_schedule(post, "facebook_page", soon(), True)

    assert db.claim(sid) is True
    assert db.claim(sid) is False, "a second claim of a running row must not win"


def test_claim_refuses_a_non_pending_row(db):
    post = db.create_post("t", "body", [])
    sid = db.create_schedule(post, "facebook_page", soon(), True)
    assert db.cancel_schedule(sid) is True
    assert db.claim(sid) is False


def test_cancel_only_applies_to_pending(db):
    post = db.create_post("t", "body", [])
    sid = db.create_schedule(post, "facebook_page", soon(), True)
    db.claim(sid)
    assert db.cancel_schedule(sid) is False, "a running schedule may already have fired"


def test_reaper_leaves_uncertain_not_failed(db):
    """The one deliberate divergence from the reaper this was modelled on, and the reason for it.

    `failed` in this project means "provably never published, safe to re-run". A publisher killed
    mid-run may already have published, so recording `failed` would invite a double post.
    """
    post = db.create_post("t", "body", [])
    sid = db.create_schedule(post, "facebook_page", soon(), True)
    db.claim(sid)

    assert db.reap_running() == [sid]

    row = next(s for s in db.list_tasks() if s["id"] == sid)
    assert row["status"] == db.UNCERTAIN
    assert row["status"] != db.FAILED
    assert sid not in [s["id"] for s in db.due_pending()], "a reaped row must not be re-queued"

    runs = db.list_runs()
    assert runs and runs[0]["status"] == db.UNCERTAIN
    assert "開機清理" in runs[0]["detail"]


def test_reaper_closes_unfinished_run_rows(db):
    post = db.create_post("t", "body", [])
    run_id = db.start_run(post, "facebook_page", False, None)
    db.reap_running()
    row = next(r for r in db.list_runs() if r["id"] == run_id)
    assert row["status"] == db.UNCERTAIN
    assert row["finished_at"] is not None, "nothing may stay non-terminal across a boot"


def test_content_floor(db, media):
    (media / "a.png").write_bytes(b"not really a png, only existence is checked")

    with pytest.raises(db.ValidationError):
        db.create_post("", "body", [])                      # no title
    with pytest.raises(db.ValidationError):
        db.create_post("t", "", [])                         # no body and no image
    with pytest.raises(db.ValidationError):
        db.create_post("t", "body", ["missing.png"])        # image not in media/
    with pytest.raises(db.ValidationError):
        db.create_post("t", "body", ["../../secret.txt"])   # escapes the media ceiling

    assert db.create_post("t", "", ["a.png"])               # image-only is a real post


def test_fields_are_trimmed(db):
    post = db.get_post(db.create_post("  標題  ", "  內文  ", []))
    assert post.title == "標題" and post.body == "內文"


def test_a_schedule_in_the_past_is_refused(db):
    post = db.create_post("t", "body", [])
    with pytest.raises(db.ValidationError):
        db.create_schedule(post, "facebook_page", "2020-01-01T09:00", True)


def test_overdue_schedules_are_missed_not_published_late(db):
    post = db.create_post("t", "body", [])
    sid = db.create_schedule(post, "facebook_page", soon(), True)
    long_ago = (datetime.now() - timedelta(days=2)).isoformat()
    with db._db() as conn:
        conn.execute("UPDATE schedules SET run_at=? WHERE id=?", (long_ago, sid))

    assert db.sweep_missed() == 1
    row = next(s for s in db.list_tasks() if s["id"] == sid)
    assert row["status"] == db.MISSED
    assert db.due_pending() == []


def test_marker_normalises_whitespace(db):
    post = db.get_post(db.create_post("t", "第一行\n\n  第二行   有空白", []))
    assert post.marker == "第一行 第二行 有空白"


# ── clearing ─────────────────────────────────────────────────────────────────────────────────
def test_clear_cannot_reach_a_task_that_has_not_finished(db):
    """The floor under the 清除 button.

    A scheduled post that quietly vanished because a button was pressed is a worse outcome than
    a cluttered list, so `pending` and `running` are excluded in SQL - not in the UI, and not by
    asking the caller nicely. Passing them explicitly must still leave them alone.
    """
    post = db.create_post("t", "body", [])
    waiting = db.create_schedule(post, "facebook_page", soon(), True)
    running = db.create_schedule(post, "line_group_note", soon(), True)
    db.claim(running)
    finished = db.create_schedule(post, "facebook_page", soon(), True)
    db.finish_schedule(finished, db.DONE)

    assert db.clear_tasks(["pending", "running", "done"]) == 1

    left = {s["id"]: s["status"] for s in db.list_tasks()}
    assert left == {waiting: db.PENDING, running: db.RUNNING}


def test_clear_takes_the_runs_with_it(db):
    post = db.create_post("t", "body", [])
    sched = db.create_schedule(post, "facebook_page", soon(), True)
    db.claim(sched)
    run = db.start_run(post, "facebook_page", True, sched)
    db.finish_schedule(sched, db.DONE)

    assert db.clear_tasks() == 1
    assert db.list_tasks() == []
    assert run not in [r["id"] for r in db.list_runs()]


def test_clearing_one_channel_keeps_the_other_s_content(db):
    """One 新增任務 with two channels is two schedules sharing ONE post row. Clearing the
    finished one must not delete the content the pending one still needs."""
    post = db.create_post("雙管道", "body", [])
    done = db.create_schedule(post, "facebook_page", soon(), True)
    still_waiting = db.create_schedule(post, "line_group_note", soon(), True)
    db.finish_schedule(done, db.DONE)

    assert db.clear_tasks() == 1
    rows = db.list_tasks()
    assert [r["id"] for r in rows] == [still_waiting]
    assert rows[0]["post_title"] == "雙管道", "its content went with the other channel's row"


def test_clearing_everything_leaves_no_orphan_posts(db):
    post = db.create_post("t", "body", [])
    sched = db.create_schedule(post, "facebook_page", soon(), True)
    db.finish_schedule(sched, db.FAILED)

    db.clear_tasks()
    assert db.list_posts() == []


def test_a_keycap_leaves_nothing_of_itself_in_the_marker(db):
    """The reason a post that really did go out was recorded as 不確定.

    「0️⃣」 is three codepoints - the ASCII digit, U+FE0F, U+20E3 - and Facebook draws all three
    as one <img>. The digit is not a pictograph by itself, so it survived into the needle and we
    searched the published page for a "0" that was not in its text. U+20E3 was not in the
    pictograph set at all, so it leaked in too.
    """
    keycap = "\N{COMBINING ENCLOSING KEYCAP}"
    vs16 = "\N{VARIATION SELECTOR-16}"

    assert db.Post(1, "t", f"tttt0{vs16}{keycap}", ()).marker == "tttt"
    assert db.Post(1, "t", f"特價2{vs16}{keycap}4{vs16}{keycap} 快來買", ()).marker == "快來買"
    assert keycap not in db.Post(1, "t", f"3{vs16}{keycap}", ()).marker


def test_the_marker_still_keeps_ordinary_text_whole(db):
    """The control: stripping must take the pictures and nothing else."""
    post = db.Post(1, "t", "Hello WORLD 大小寫測試 🎉 收工", ())
    assert post.marker == "Hello WORLD 大小寫測試"


def test_a_zero_width_joiner_sequence_goes_entirely(db):
    zwj = "\u200d"
    body = f"家庭日 👨{zwj}👩{zwj}👧 開始報名"
    assert db.Post(1, "t", body, ()).marker == "開始報名"
