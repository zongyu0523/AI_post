"""The app: a local web UI, a scheduler tick, the boot reaper, and live run streaming.

Run:  python -m uvicorn app:app --port 8800    (then open http://127.0.0.1:8800)
"""
from __future__ import annotations

import asyncio
import json
import re
import traceback
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import channels
import config
from channels import line_note
import linewin
import llm
import store
from config import MEDIA_DIR, ROOT, SHOTS_DIR, TICK_SECONDS
from gate import Attempt, Outcome, settle

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
SCHEDULE_STATUS = {"success": store.DONE, "uncertain": store.UNCERTAIN,
                   "failed": store.FAILED, "stopped": store.STOPPED}

# A lock per RUNTIME, not one for the whole app. Two browser runs would fight over the Chrome
# profile's exclusive lock, and two LINE runs would fight over the one LINE window - but Facebook
# and LINE share nothing, so they run at the same time.
RUN_LOCKS: dict[str, asyncio.Lock] = {}


def runtime_lock(runtime: str) -> asyncio.Lock:
    return RUN_LOCKS.setdefault(runtime, asyncio.Lock())


@dataclass
class LiveRun:
    run_id: int
    post_title: str
    channel: str
    dry_run: bool
    attempt: Attempt
    started_at: str
    waiting: bool = True          # queued behind this runtime's lock
    outcome: Outcome | None = None

    def stop(self) -> None:
        """Ask it to stop. It lands at the next step boundary, never mid-click."""
        self.attempt.request_stop()


LIVE: dict[int, LiveRun] = {}


# ───────────────────────────────────────────────────────────────────────── running a post
async def execute(run_id: int, post_id: int, channel_key: str, dry_run: bool,
                  schedule_id: int | None = None) -> Outcome:
    channel = channels.get(channel_key)
    post = store.get_post(post_id)
    attempt = Attempt(channel=channel_key, dry_run=dry_run)
    live = LiveRun(run_id=run_id, post_title=post.title, channel=channel_key,
                   dry_run=dry_run, attempt=attempt, started_at=store.now_iso())
    LIVE[run_id] = live
    error: Exception | None = None

    lock = runtime_lock(channel.runtime)
    try:
        if lock.locked():
            # One Chrome profile, one LINE window: the queue is per runtime, not per channel.
            busy = "瀏覽器" if channel.runtime == "browser" else "電腦上的 LINE"
            attempt.step(f"⏳ 排隊中：等前一個用{busy}的工作跑完…")
        async with lock:
            live.waiting = False
            attempt.step(f"▶ 開始 · {channel.label} · {'預演' if dry_run else '真的發'}")
            try:
                await channel.publish(post, attempt, dry_run=dry_run,
                                      prefix=f"{channel_key}-{run_id}")
            except Exception as exc:
                error = exc
                attempt.step(f"✖ {type(exc).__name__}: {exc}")

        # A hard kill between here and the writes below leaves the row `running`; that is what
        # store.reap_running() is for, and why it reaps to `uncertain` rather than `failed`.
        outcome = settle(attempt, error)
        store.finish_run(run_id, outcome)
        if schedule_id is not None:
            store.finish_schedule(schedule_id, SCHEDULE_STATUS[outcome.status])
        live.outcome = outcome
        attempt.step(f"■ 結束: {outcome.status} — {outcome.detail}")
        return outcome
    finally:
        # Linger briefly so a watching client receives the final frames before the stream ends.
        asyncio.get_running_loop().call_later(20, LIVE.pop, run_id, None)


async def ticker() -> None:
    while True:
        try:
            store.sweep_missed()
            for row in store.due_pending():
                if store.claim(row["id"]):
                    run_id = store.start_run(row["post_id"], row["channel"],
                                             bool(row["dry_run"]), row["id"])
                    # Fire and forget: a Facebook run must not hold up a LINE one that is also
                    # due. Each still waits on its own runtime lock inside execute().
                    asyncio.create_task(execute(run_id, row["post_id"], row["channel"],
                                                bool(row["dry_run"]), row["id"]))
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(TICK_SECONDS)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    config.ensure_dirs()
    store.init()
    reaped = store.reap_running()
    if reaped:
        print(f"[boot] 清理了 {len(reaped)} 個卡在 running 的排程 -> uncertain: {reaped}")
    task = asyncio.create_task(ticker())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def no_store_api(request: Request, call_next):
    """Never let an API response be cached. Learned the hard way elsewhere: an API without
    Cache-Control got cached and produced failures that looked like CORS."""
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(store.ValidationError)
async def _validation(_r: Request, exc: store.ValidationError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(channels.ChannelNotReady)
async def _not_ready(_r: Request, exc: channels.ChannelNotReady):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(channels.UnknownChannel)
async def _unknown_channel(_r: Request, exc: channels.UnknownChannel):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(llm.LLMError)
async def _llm_error(_r: Request, exc: llm.LLMError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(linewin.LineNotAvailable)
async def _no_line(_r: Request, exc: linewin.LineNotAvailable):
    return JSONResponse({"detail": str(exc)}, status_code=409)


# ─────────────────────────────────────────────────────────────────────────────── payloads
class PostIn(BaseModel):
    title: str = ""
    body: str = ""
    images: list[str] = []


class ScheduleIn(BaseModel):
    post_id: int
    channel: str
    run_at: str
    dry_run: bool = True


class RunIn(BaseModel):
    post_id: int
    channel: str
    dry_run: bool = True


class ClearIn(BaseModel):
    """Which finished tasks to clear. Empty = every finished one. `store.clear_tasks` is what
    actually refuses to touch a pending or running task, so this stays a plain list."""
    statuses: list[str] = []


class TaskIn(BaseModel):
    """One task = one piece of content, sent to one or more channels.

    Posts and schedules stay separate rows underneath (the content is shared, the firing is
    per-channel, and each firing needs its own status), but there is no reason to make anyone
    assemble them by hand.
    """
    channels: list[str]
    title: str = ""
    body: str = ""
    images: list[str] = []
    run_at: str = ""            # empty = run now
    dry_run: bool = True


class SettingsIn(BaseModel):
    values: dict[str, str]


# ─────────────────────────────────────────────────────────────────────────────── read API
@app.get("/api/state")
def state() -> dict:
    try:
        win = linewin.find_window()
        line_window = {"width": win.width, "height": win.height}
    except linewin.LineNotAvailable as exc:
        line_window = {"error": str(exc)}

    settings = store.all_settings()
    settings.pop("llm_api_key", None)              # never echo the key back

    return {
        "tasks": store.list_tasks(),
        "channels": channels.describe(),
        "settings": settings,
        "llm": llm.load_config().masked(),
        "media": media_files(),
        "live": {str(lr.run_id): {"waiting": lr.waiting,
                                  "done": lr.outcome is not None,
                                  "stopping": lr.attempt.stop_requested,
                                  "status": lr.outcome.status if lr.outcome else "running",
                                  "steps": list(lr.attempt.steps)}
                 for lr in LIVE.values()},
        "line": {"window": line_window, "learned": line_note.learned()},
    }


def media_files() -> list[str]:
    if not MEDIA_DIR.exists():
        return []
    return sorted(p.name for p in MEDIA_DIR.iterdir()
                  if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)


@app.get("/api/runs/{run_id}/stream")
async def stream_run(run_id: int) -> StreamingResponse:
    """Server-sent events: every step as the run produces it."""
    async def frames():
        sent = 0
        idle = 0
        while idle < 1800:                         # ~15 min ceiling
            live = LIVE.get(run_id)
            if live is None:
                yield "event: end\ndata: {}\n\n"
                return
            steps = live.attempt.steps
            while sent < len(steps):
                payload = json.dumps({"i": sent, "text": steps[sent]}, ensure_ascii=False)
                yield f"data: {payload}\n\n"
                sent += 1
                idle = 0
            if live.outcome is not None:
                done = json.dumps({"status": live.outcome.status,
                                   "detail": live.outcome.detail,
                                   "stage": live.outcome.stage.value}, ensure_ascii=False)
                yield f"event: end\ndata: {done}\n\n"
                return
            idle += 1
            await asyncio.sleep(0.5)
        yield "event: end\ndata: {}\n\n"

    return StreamingResponse(frames(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# ────────────────────────────────────────────────────────────────────────────── write API
@app.post("/api/posts")
def create_post(payload: PostIn) -> dict:
    return {"id": store.create_post(payload.title, payload.body, payload.images)}


@app.put("/api/posts/{post_id}")
def update_post(post_id: int, payload: PostIn) -> dict:
    store.update_post(post_id, payload.title, payload.body, payload.images)
    return {"ok": True}


@app.delete("/api/posts/{post_id}")
def delete_post(post_id: int) -> dict:
    store.delete_post(post_id)
    return {"ok": True}


@app.post("/api/schedules")
def create_schedule(payload: ScheduleIn) -> dict:
    channel = channels.get(payload.channel)
    channel.check_post(store.get_post(payload.post_id))
    return {"id": store.create_schedule(payload.post_id, payload.channel,
                                        payload.run_at, payload.dry_run)}


@app.post("/api/schedules/{schedule_id}/cancel")
def cancel_schedule(schedule_id: int) -> dict:
    if not store.cancel_schedule(schedule_id):
        raise HTTPException(409, "這個排程已經不是 pending 了，取消不了")
    return {"ok": True}


@app.post("/api/tasks/clear")
def clear_tasks(payload: ClearIn) -> dict:
    """Clear finished tasks. A live run holds its own LIVE entry, which expires on its own."""
    return {"cleared": store.clear_tasks(payload.statuses or None)}


@app.post("/api/tasks")
async def create_task(payload: TaskIn) -> dict:
    """Create the content once and fire it at every chosen channel, now or later."""
    if not payload.channels:
        raise HTTPException(400, "至少要選一個頻道")

    title = payload.title.strip() or " ".join(payload.body.split())[:28] or "未命名任務"
    chosen = [channels.get(key) for key in payload.channels]      # unknown key -> 400
    post_id = store.create_post(title, payload.body, payload.images)
    post = store.get_post(post_id)
    for channel in chosen:
        channel.check_post(post)                                   # producing-end floor

    # "Run now" is a schedule for now, not a separate kind of thing: one row per task means the
    # task you just started is in the same list as the ones you scheduled.
    when = payload.run_at.strip() or store.now_iso()
    tasks, runs = [], []
    for channel in chosen:
        schedule_id = store.create_schedule(post_id, channel.key, when, payload.dry_run)
        tasks.append(schedule_id)
        if not payload.run_at.strip() and store.claim(schedule_id):
            run_id = store.start_run(post_id, channel.key, payload.dry_run, schedule_id)
            asyncio.create_task(execute(run_id, post_id, channel.key,
                                        payload.dry_run, schedule_id))
            runs.append(run_id)
    return {"post_id": post_id, "tasks": tasks, "runs": runs}


@app.post("/api/run")
async def run_now(payload: RunIn) -> dict:
    """Starts the run and returns immediately with its id, so the UI can stream it."""
    channel = channels.get(payload.channel)
    channel.check_post(store.get_post(payload.post_id))
    run_id = store.start_run(payload.post_id, payload.channel, payload.dry_run, None)
    asyncio.create_task(execute(run_id, payload.post_id, payload.channel, payload.dry_run))
    return {"run_id": run_id}


SAFE_NAME = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]")
MAX_IMAGE_BYTES = 12 * 1024 * 1024


@app.post("/api/media")
async def upload_media(files: list[UploadFile] = File(...)) -> dict:
    """Take images from a file picker and keep them where the channels can reach them.

    The folder is an implementation detail: a browser needs a path to hand to `set_input_files`,
    so the bytes have to land on disk somewhere. Names are sanitised and de-duplicated rather than
    trusted - an upload name is attacker-controlled in general, and here it is at least
    user-controlled enough to contain a slash by accident.
    """
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    saved, skipped = [], []
    for item in files:
        raw = Path(item.filename or "").name
        suffix = Path(raw).suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            skipped.append(f"{raw}（不是圖片）")
            continue
        body = await item.read()
        if len(body) > MAX_IMAGE_BYTES:
            skipped.append(f"{raw}（超過 12MB）")
            continue

        stem = SAFE_NAME.sub("_", Path(raw).stem)[:60] or "image"
        name = f"{stem}{suffix}"
        n = 2
        while (MEDIA_DIR / name).exists():
            name = f"{stem}-{n}{suffix}"
            n += 1
        (MEDIA_DIR / name).write_bytes(body)
        saved.append(name)
    return {"saved": saved, "skipped": skipped}


@app.delete("/api/media/{name}")
def delete_media(name: str) -> dict:
    target = (MEDIA_DIR / Path(name).name).resolve()
    if target.is_relative_to(MEDIA_DIR.resolve()) and target.is_file():
        target.unlink()
    return {"ok": True}


@app.post("/api/runs/{run_id}/stop")
def stop_run(run_id: int) -> dict:
    live = LIVE.get(run_id)
    if live is None or live.outcome is not None:
        raise HTTPException(409, "這個執行已經結束了")
    live.stop()
    return {"ok": True}


@app.post("/api/settings")
def save_settings(payload: SettingsIn) -> dict:
    for key, value in payload.values.items():
        store.set_setting(key, value)
    return {"ok": True}


@app.post("/api/llm/test")
async def llm_test() -> dict:
    cfg = llm.load_config()
    reply = await llm.probe(cfg)
    return {"ok": True, "model": cfg.model, "reply": reply}


@app.post("/api/facebook/browser")
async def facebook_browser() -> dict:
    """Open the dedicated Chrome and hold it open until the user closes the window.

    It takes the browser lock for as long as it is open: one Chrome profile cannot be driven by
    two things at once, and a scheduled post that collided with it would die on a profile lock.
    Holding it makes a due post queue instead, which the task list shows as 排隊中.
    """
    from channels.facebook_page import FacebookPage

    lock = runtime_lock("browser")
    if lock.locked():
        raise HTTPException(409, "現在有一個發文工作在用瀏覽器，等它跑完再開")
    async with lock:
        return await FacebookPage().open_browser()


# ──────────────────────────────────────────────────────────────────────────── static bits
@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


config.ensure_dirs()
app.mount("/shots", StaticFiles(directory=SHOTS_DIR), name="shots")
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
