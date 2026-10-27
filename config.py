"""Paths and the two knobs that change behaviour. Everything else is in the code that uses it."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent

MEDIA_DIR = ROOT / "media"          # the folder you drop images into
DATA_DIR = ROOT / "data"            # db, browser profiles, screenshots - all gitignored
SHOTS_DIR = DATA_DIR / "shots"      # evidence screenshots, served at /shots/
PROFILE_DIR = DATA_DIR / "profiles"  # one persistent Chrome profile per browser channel
DB_PATH = DATA_DIR / "ai_post.sqlite3"
LINE_RECIPE = DATA_DIR / "line_recipe.json"        # the taught click path
LINE_TEMPLATES = DATA_DIR / "line_templates"       # one PNG per thing to click

TICK_SECONDS = 20

# A schedule that came due while the app was closed is NOT published late: past this many minutes
# it is marked `missed`. Posting yesterday's content at random tomorrow is a worse failure than
# not posting it, and "the app was off" is the normal case for a desktop tool.
GRACE_MINUTES = 60


def ensure_dirs() -> None:
    for d in (MEDIA_DIR, DATA_DIR, SHOTS_DIR, PROFILE_DIR, LINE_TEMPLATES):
        d.mkdir(parents=True, exist_ok=True)
