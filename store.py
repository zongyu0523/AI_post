"""SQLite store: posts, one-shot schedules, the run log, settings.

SQLite rather than a JSON file because these rows ARE the record of "did we publish this?".
An earlier project paid for what a JSON default costs once it
quietly becomes the store of record.

Times are naive LOCAL ISO-8601 ("2026-10-24T20:30:00"). This is a single-machine tool and you think
in local time; round-tripping through UTC is the usual source of "it posted an hour late".
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from config import DB_PATH, GRACE_MINUTES, MEDIA_DIR
from gate import Outcome

# schedule statuses
PENDING = "pending"
RUNNING = "running"
DONE = "done"           # published, with evidence
UNCERTAIN = "uncertain"  # may be live - a human looks
FAILED = "failed"        # provably never published - safe to re-run
MISSED = "missed"        # came due while the app was closed, past the grace window
CANCELED = "canceled"
STOPPED = "stopped"      # the user pressed stop; nothing was published

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS posts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    images     TEXT NOT NULL DEFAULT '[]',   -- JSON array of media-relative paths
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id    INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    channel    TEXT NOT NULL,
    run_at     TEXT NOT NULL,
    dry_run    INTEGER NOT NULL DEFAULT 1,
    status     TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules(status, run_at);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER,
    post_id     INTEGER NOT NULL,
    channel     TEXT NOT NULL,
    dry_run     INTEGER NOT NULL,
    status      TEXT NOT NULL,
    stage       TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    evidence    TEXT,
    steps       TEXT NOT NULL DEFAULT '[]',
    started_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_recent ON runs(started_at DESC);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class ValidationError(ValueError):
    """Rejected at the write boundary, so a bad post can never reach a publish attempt."""


@dataclass(frozen=True)
class Post:
    id: int
    title: str
    body: str
    images: tuple[str, ...]

    def image_paths(self) -> list[Path]:
        return [MEDIA_DIR / rel for rel in self.images]

    @property
    def marker(self) -> str:
        """The substring we look for when reading the post back.

        Whitespace-normalised, because a rich-text editor imposes its own line handling - and
        stripped of emoji and other pictographs, because Facebook renders those as <img> elements,
        so they are simply absent from the page text we search. A needle containing 🎉 can never
        match a published post, which is how a post that went out fine was recorded as uncertain.
        """
        # The longest unbroken stretch, not the pieces spliced together: splicing invents a join
        # that is not on the page, and whatever sat next to the emoji may be spaced differently
        # once the <img> is gone.
        return max(text_runs(self.body), key=len, default="")[:40]


def text_runs(body: str) -> list[str]:
    """`body` split at every character Facebook draws as an <img>, in order.

    One definition, used twice: `Post.marker` takes the longest run to search a published page
    for, and the composer's read-back check compares runs so that an editor showing 「ab」 for
    「a🎉b」 is recognised as correct rather than as text that needs retyping.
    """
    runs, current = [], []
    for i, ch in enumerate(body or ""):
        following = body[i + 1] if i + 1 < len(body) else ""
        # A keycap is THREE codepoints - digit + U+FE0F + U+20E3 - drawn as one image. The digit
        # is an ordinary ASCII character, so without this it survives into the needle and we hunt
        # a published post for a "2" the page does not contain.
        if _PICTOGRAPH(ch) or (following and _GLUES_TO_PREVIOUS(following)):
            runs.append("".join(current))
            current = []
        else:
            current.append(ch)
    runs.append("".join(current))
    return [" ".join(r.split()) for r in runs if r.strip()]


def _PICTOGRAPH(ch: str) -> bool:
    """Characters Facebook draws as an <img> instead of as text.

    This used to say "anything above U+FFFF", which is far more than the emoji. Styled letters
    like 𝗍𝗲𝘁𝘀𝗲𝘁 (Mathematical Alphanumeric Symbols, U+1D400-U+1D7FF) and rare CJK (U+20000 and
    up) are TEXT - Facebook renders them as the letters they are - and stripping them took a real
    post's needle down to the single character "t". So the emoji blocks, and nothing else above
    the BMP.
    """
    code = ord(ch)
    return (0x1F000 <= code <= 0x1FAFF          # the emoji blocks
            or 0x2190 <= code <= 0x2BFF         # arrows, misc symbols, dingbats
            or 0xFE00 <= code <= 0xFE0F         # variation selectors
            or code == 0x20E3                   # combining enclosing keycap
            or code == 0x200D)                  # zero-width joiner


def _GLUES_TO_PREVIOUS(ch: str) -> bool:
    """Marks that turn the character BEFORE them into part of a picture.

    U+FE0F makes the preceding character render as emoji; U+20E3 encloses it in a keycap; a ZWJ
    binds it into a sequence. In every case the page shows one <img> and none of the characters.
    """
    return ord(ch) in (0xFE0F, 0x20E3, 0x200D)


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _db() as conn:
        conn.executescript(SCHEMA)


# ─────────────────────────────────────────────────────────── validation (the producing end)
def resolve_media(rel: str) -> Path:
    """Resolve a media-relative path, refusing anything that escapes MEDIA_DIR.

    The local folder is the ceiling, the same shape as an earlier project's rule
    for the same reason: a path that leaves it is rejected here rather
    than trusted because it came from our own UI.
    """
    rel = rel.strip().replace("\\", "/").lstrip("/")
    if not rel:
        raise ValidationError("圖片路徑是空的")
    base = MEDIA_DIR.resolve()
    target = (base / rel).resolve()
    if not target.is_relative_to(base):
        raise ValidationError(f"圖片必須在 media/ 底下: {rel}")
    if not target.is_file():
        raise ValidationError(f"找不到圖片: {rel}")
    return target


def validate_post(title: str, body: str, images: list[str]) -> tuple[str, str, list[str]]:
    """Trim and check. Called on every write, so the DB never holds an unpublishable post.

    Fields are trimmed on BOTH ends - here and in the UI - after a case elsewhere
    where an untrimmed host turned into a DNS failure.

    Note what is deliberately NOT checked: the `reads_as_unfilled` floor from
    `workflow-unfilled-report-passes-string-floor.md` (rejecting bodies like "not retrieved").
    That floor exists for values a *model* produced when it had nothing; this body is typed by a
    human, and the same lesson says an author's literal text must not be blocked for quoting those
    words. Applying it here would be cargo-culting it into the one place it says not to go.
    """
    title = (title or "").strip()
    body = (body or "").strip()
    images = [i.strip() for i in (images or []) if i and i.strip()]

    if not title:
        raise ValidationError("標題不能空白")
    if not body and not images:
        raise ValidationError("內文跟圖片至少要有一個")
    for rel in images:
        resolve_media(rel)
    return title, body, images


def parse_run_at(raw: str) -> str:
    """Accept what <input type="datetime-local"> sends, and refuse a time already gone."""
    raw = (raw or "").strip()
    if not raw:
        raise ValidationError("排程時間不能空白")
    try:
        when = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError(f"看不懂的時間格式: {raw}") from exc
    if when < datetime.now() - timedelta(minutes=1):
        raise ValidationError("排程時間已經過了")
    return when.replace(second=0, microsecond=0).isoformat()


# ─────────────────────────────────────────────────────────────────────────────── posts
def _row_to_post(row: sqlite3.Row) -> Post:
    return Post(id=row["id"], title=row["title"], body=row["body"],
                images=tuple(json.loads(row["images"])))


def create_post(title: str, body: str, images: list[str]) -> int:
    title, body, images = validate_post(title, body, images)
    ts = now_iso()
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO posts (title, body, images, created_at, updated_at) VALUES (?,?,?,?,?)",
            (title, body, json.dumps(images, ensure_ascii=False), ts, ts))
        return int(cur.lastrowid)


def update_post(post_id: int, title: str, body: str, images: list[str]) -> None:
    title, body, images = validate_post(title, body, images)
    with _db() as conn:
        cur = conn.execute(
            "UPDATE posts SET title=?, body=?, images=?, updated_at=? WHERE id=?",
            (title, body, json.dumps(images, ensure_ascii=False), now_iso(), post_id))
        if cur.rowcount != 1:
            raise ValidationError(f"找不到貼文 {post_id}")


def delete_post(post_id: int) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM posts WHERE id=?", (post_id,))


def get_post(post_id: int) -> Post:
    with _db() as conn:
        row = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
    if row is None:
        raise ValidationError(f"找不到貼文 {post_id}")
    return _row_to_post(row)


def list_posts() -> list[dict]:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM posts ORDER BY updated_at DESC").fetchall()
    return [{"id": r["id"], "title": r["title"], "body": r["body"],
             "images": json.loads(r["images"]), "updated_at": r["updated_at"]} for r in rows]


# ─────────────────────────────────────────────────────────────────────────── schedules
def create_schedule(post_id: int, channel: str, run_at: str, dry_run: bool) -> int:
    get_post(post_id)  # existence check at the write boundary
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO schedules (post_id, channel, run_at, dry_run, status, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (post_id, channel.strip(), parse_run_at(run_at), int(bool(dry_run)), PENDING,
             now_iso()))
        return int(cur.lastrowid)


def cancel_schedule(schedule_id: int) -> bool:
    """Only a pending schedule can be cancelled - a running one may already have fired."""
    with _db() as conn:
        cur = conn.execute("UPDATE schedules SET status=? WHERE id=? AND status=?",
                           (CANCELED, schedule_id, PENDING))
        return cur.rowcount == 1


def list_tasks(limit: int = 120) -> list[dict]:
    """One row per task: the schedule, its content, and whatever its latest run knows.

    The UI shows a single list rather than a schedule list plus a separate history, so the join
    happens here instead of in the browser.
    """
    with _db() as conn:
        rows = conn.execute(
            "SELECT s.*, p.title AS post_title, p.body AS post_body, p.images AS post_images, "
            "       r.id AS run_id, r.status AS run_status, r.stage AS run_stage, "
            "       r.detail AS run_detail, r.evidence AS run_evidence, r.steps AS run_steps, "
            "       r.started_at AS run_started_at, r.finished_at AS run_finished_at "
            "FROM schedules s "
            "JOIN posts p ON p.id = s.post_id "
            "LEFT JOIN runs r ON r.id = ("
            "    SELECT id FROM runs WHERE schedule_id = s.id ORDER BY id DESC LIMIT 1) "
            "ORDER BY s.id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["post_images"] = json.loads(row["post_images"] or "[]")
        item["run_steps"] = json.loads(row["run_steps"] or "[]")
        item["run_evidence"] = json.loads(row["run_evidence"]) if row["run_evidence"] else None
        out.append(item)
    return out


FINISHED = (DONE, UNCERTAIN, FAILED, MISSED, CANCELED, STOPPED)


def clear_tasks(statuses: list[str] | None = None) -> int:
    """Delete finished tasks (and their runs). Returns how many went.

    `pending` and `running` are not in FINISHED and are therefore unreachable from here, whatever
    the caller passes: a task that has not had its turn yet cannot be cleared by accident, and a
    running one must not be deleted out from under the thing that is running it.
    """
    wanted = [s for s in (statuses or FINISHED) if s in FINISHED]
    if not wanted:
        return 0
    marks = ",".join("?" * len(wanted))
    with _db() as conn:
        ids = [r["id"] for r in conn.execute(
            f"SELECT id FROM schedules WHERE status IN ({marks})", wanted).fetchall()]
        if not ids:
            return 0
        holes = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM runs WHERE schedule_id IN ({holes})", ids)
        conn.execute(f"DELETE FROM schedules WHERE id IN ({holes})", ids)
        # A post outlives its schedule only while another schedule still points at it.
        conn.execute("DELETE FROM posts WHERE id NOT IN (SELECT post_id FROM schedules) "
                     "AND id NOT IN (SELECT post_id FROM runs)")
        return len(ids)


def sweep_missed(now: datetime | None = None) -> int:
    """Mark schedules that came due while the app was closed. See GRACE_MINUTES in config."""
    cutoff = ((now or datetime.now()) - timedelta(minutes=GRACE_MINUTES)).isoformat()
    with _db() as conn:
        cur = conn.execute("UPDATE schedules SET status=? WHERE status=? AND run_at < ?",
                           (MISSED, PENDING, cutoff))
        return cur.rowcount


def due_pending(now: datetime | None = None) -> list[dict]:
    stamp = (now or datetime.now()).isoformat()
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM schedules WHERE status=? AND run_at <= ? ORDER BY run_at",
            (PENDING, stamp)).fetchall()
    return [dict(r) for r in rows]


def claim(schedule_id: int) -> bool:
    """Compare-and-set pending -> running. True only if THIS call made the transition.

    The guard names the source status on purpose. An earlier project's lesson
    says a guard that can be satisfied
    by the state you are trying to leave (parked -> parked still reports UPDATE 1) is not a guard.
    Here a second claim of an already-running row changes nothing, so rowcount is 0 and the caller
    knows it did not win.
    """
    with _db() as conn:
        cur = conn.execute("UPDATE schedules SET status=? WHERE id=? AND status=?",
                           (RUNNING, schedule_id, PENDING))
        return cur.rowcount == 1


def finish_schedule(schedule_id: int, status: str) -> None:
    with _db() as conn:
        conn.execute("UPDATE schedules SET status=? WHERE id=?", (status, schedule_id))


def reap_running(reason: str = "程式在這個 run 跑到一半時結束了") -> list[int]:
    """Boot-time reconciliation of rows a dead process left mid-flight.

    Learned the hard way elsewhere: a terminal write that lives only in
    an in-process `finally` is not durable across process death, and a non-terminal flag with no
    reaper becomes a permanent zombie. Safe here for the same reason it was safe there - single
    process, so at boot there are no live producers by definition.

    One deliberate divergence: that reaper flips to `failed`, matching its producer's own crash
    path. This one flips to `uncertain`, because a publisher that died mid-run may already have
    published. In this project `failed` means "provably never fired, safe to re-run" - recording
    that here would invite exactly the double-post the gate exists to prevent.
    """
    ts = now_iso()
    with _db() as conn:
        rows = conn.execute("SELECT * FROM schedules WHERE status=?", (RUNNING,)).fetchall()
        for r in rows:
            conn.execute(
                "INSERT INTO runs (schedule_id, post_id, channel, dry_run, status, stage, detail, "
                "steps, started_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (r["id"], r["post_id"], r["channel"], r["dry_run"], UNCERTAIN, "unknown",
                 f"開機清理: {reason}", "[]", ts, ts))
        conn.execute("UPDATE schedules SET status=? WHERE status=?", (UNCERTAIN, RUNNING))
        conn.execute(
            "UPDATE runs SET status=?, stage=?, detail=?, finished_at=? "
            "WHERE status=? AND finished_at IS NULL",
            (UNCERTAIN, "unknown", f"開機清理: {reason}", ts, RUNNING))
    return [r["id"] for r in rows]


# ───────────────────────────────────────────────────────────────────────────── run log
def start_run(post_id: int, channel: str, dry_run: bool, schedule_id: int | None) -> int:
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO runs (schedule_id, post_id, channel, dry_run, status, stage, started_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (schedule_id, post_id, channel, int(bool(dry_run)), RUNNING, "not_fired", now_iso()))
        return int(cur.lastrowid)


def finish_run(run_id: int, outcome: Outcome) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE runs SET status=?, stage=?, detail=?, evidence=?, steps=?, finished_at=? "
            "WHERE id=?",
            (outcome.status, outcome.stage.value, outcome.detail,
             outcome.evidence.to_json() if outcome.evidence else None,
             json.dumps(list(outcome.steps), ensure_ascii=False), now_iso(), run_id))


def list_runs(limit: int = 50) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT r.*, p.title AS post_title FROM runs r "
            "LEFT JOIN posts p ON p.id = r.post_id ORDER BY r.id DESC LIMIT ?",
            (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["evidence"] = json.loads(r["evidence"]) if r["evidence"] else None
        d["steps"] = json.loads(r["steps"] or "[]")
        out.append(d)
    return out


# ───────────────────────────────────────────────────────────────────────────── settings
def get_setting(key: str, default: str = "") -> str:
    with _db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with _db() as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES (?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, (value or "").strip()))


def all_settings() -> dict[str, str]:
    with _db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}
