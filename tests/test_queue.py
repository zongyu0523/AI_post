"""One browser, one job at a time - and the waiting has to be visible.

There is exactly one Chrome profile per channel, so two Facebook posts cannot run at once. The
queue is an `asyncio.Lock` per RUNTIME ("browser" / "desktop"), which is FIFO in CPython: waiters
are woken in the order they arrived.

Per runtime, not per channel, is the point: a Facebook post and a LINE post use different
machinery and must not block each other, while two Facebook posts must.

The second thing tested here is that waiting LOOKS like waiting. A queued task that rendered as
執行中 would read as a hang, which is how someone ends up killing the app and losing the run.
"""
import asyncio

import pytest

import app
import channels
from channels.base import Channel
from gate import Attempt


class Slow(Channel):
    """A channel that records when it ran, and holds its runtime for a moment."""

    def __init__(self, key, runtime, log, hold=0.25):
        self.key, self.runtime, self.label = key, runtime, key
        self.log, self.hold = log, hold

    async def publish(self, post, attempt: Attempt, *, dry_run: bool, prefix: str) -> None:
        self.log.append(("start", self.key))
        await asyncio.sleep(self.hold)
        self.log.append(("end", self.key))
        attempt.prove("feed_text_match", post.marker, "")


@pytest.fixture()
def wired(db, monkeypatch):
    """Point the executor at throwaway channels and a throwaway database."""
    log = []
    monkeypatch.setattr(app, "store", db)
    monkeypatch.setattr(app, "RUN_LOCKS", {})
    monkeypatch.setattr(app, "LIVE", {})
    fakes = {"fb_a": Slow("fb_a", "browser", log), "fb_b": Slow("fb_b", "browser", log),
             "line_x": Slow("line_x", "desktop", log)}
    monkeypatch.setattr(channels, "get", lambda key: fakes[key])
    return db, log


def _run(db, *keys):
    post = db.create_post("t", "內文", [])
    runs = [db.start_run(post, key, True, None) for key in keys]

    async def go():
        await asyncio.gather(*(app.execute(run, post, key, True)
                               for run, key in zip(runs, keys)))

    asyncio.run(go())
    return runs


def test_two_browser_jobs_never_overlap(wired):
    db, log = wired
    _run(db, "fb_a", "fb_b")

    assert log == [("start", "fb_a"), ("end", "fb_a"),
                   ("start", "fb_b"), ("end", "fb_b")], log


def test_the_queue_is_first_in_first_out(wired):
    """asyncio.Lock wakes waiters in arrival order, so the task that came due first goes first."""
    db, log = wired
    monkey = ["fb_a", "fb_b", "fb_a", "fb_b"]
    db2, _ = wired
    _run(db, *monkey)
    starts = [key for kind, key in log if kind == "start"]
    assert starts == monkey, starts


def test_facebook_does_not_block_line(wired):
    """Different runtimes, different queues - otherwise one slow channel stalls the other."""
    db, log = wired
    _run(db, "fb_a", "line_x")

    # both started before either finished: they really did overlap
    assert log[0][0] == "start" and log[1][0] == "start", log


def test_a_queued_task_says_it_is_queued(wired):
    """`waiting` is what the UI renders as 排隊中. It must be true while queued and false once
    the task actually holds the browser."""
    db, log = wired
    post = db.create_post("t", "內文", [])
    first = db.start_run(post, "fb_a", True, None)
    second = db.start_run(post, "fb_b", True, None)
    seen = {}

    async def go():
        task = asyncio.ensure_future(app.execute(first, post, "fb_a", True))
        await asyncio.sleep(0.05)                       # let the first one take the lock
        queued = asyncio.ensure_future(app.execute(second, post, "fb_b", True))
        await asyncio.sleep(0.05)
        seen["while_queued"] = app.LIVE[second].waiting
        seen["while_running"] = app.LIVE[first].waiting
        await asyncio.gather(task, queued)
        seen["after"] = app.LIVE[second].waiting

    asyncio.run(go())
    assert seen == {"while_queued": True, "while_running": False, "after": False}


def test_the_queued_one_says_so_in_its_steps(wired):
    """And it says why, in words, before anything else happens to it."""
    db, log = wired
    post = db.create_post("t", "內文", [])
    first = db.start_run(post, "fb_a", True, None)
    second = db.start_run(post, "fb_b", True, None)

    async def go():
        task = asyncio.ensure_future(app.execute(first, post, "fb_a", True))
        await asyncio.sleep(0.05)
        await app.execute(second, post, "fb_b", True)
        await task

    asyncio.run(go())
    steps = [r for r in db.list_runs() if r["id"] == second][0]["steps"]
    assert any("排隊中" in s for s in steps), steps
    assert steps.index(next(s for s in steps if "排隊中" in s)) == 0,         "it has to say it is queued BEFORE it looks like it started"
