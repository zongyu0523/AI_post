import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """A store bound to a throwaway database and media folder.

    `store` imports its paths by value, so the module attributes are what has to be patched.
    """
    import store

    media = tmp_path / "media"
    media.mkdir()
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.sqlite3")
    monkeypatch.setattr(store, "MEDIA_DIR", media)
    store.init()
    return store


@pytest.fixture()
def media(db):
    return db.MEDIA_DIR
