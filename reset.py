"""交付前清乾淨：把這份複本裡屬於「某個人」的東西全部刪掉。

這個資料夾裡有你的 Facebook 登入狀態 —— `data/profiles/` 是登入後的 Chrome 設定檔，
拿到它的人可以直接用你的帳號發文。原封不動寄出去等於把帳號一起寄出去。

會先列出要刪什麼、各佔多少空間，問過才動手。

    python reset.py            列出來，問 y/n
    python reset.py --yes      不問直接刪（寫在腳本裡用的）

**在你要交付的那份複本上跑**，不要在自己平常用的那份跑。
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
MEDIA = ROOT / "media"


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _rows(db: Path) -> str:
    """How much history the database is holding, so the number is not a guess."""
    if not db.exists():
        return "（沒有資料庫）"
    try:
        with sqlite3.connect(db) as conn:
            counts = {t: conn.execute(f"select count(*) from {t}").fetchone()[0]
                      for t in ("posts", "schedules", "runs", "settings")}
        return "、".join(f"{k} {v} 筆" for k, v in counts.items())
    except sqlite3.Error as exc:
        return f"（讀不到：{exc}）"


TARGETS = [
    (DATA / "profiles", "Chrome 設定檔", "⚠️ 你的 Facebook 登入狀態就在這裡"),
    (DATA / "shots", "執行截圖", "裡面是你的 Facebook 畫面"),
    (DATA / "ai_post.sqlite3", "資料庫", ""),
    (DATA / "line_templates", "LINE 模板圖", "你錄的 LINE 介面截圖"),
    (DATA / "line_recipe.json", "LINE 步驟設定", ""),
    (MEDIA, "圖片", "你放進去要發的圖（資料夾會留著）"),
]


def main() -> int:
    found = [(p, label, note) for p, label, note in TARGETS if p.exists()]
    if not found:
        print("已經是乾淨的了，沒有東西要刪。")
        return 0

    print("要刪掉這些：\n")
    for path, label, note in found:
        detail = _rows(path) if path.name.endswith(".sqlite3") else _human(_size(path))
        print(f"  {label:<14} {path.relative_to(ROOT)}")
        print(f"  {'':<14} {detail}" + (f"   {note}" if note else ""))
        print()
    print("程式碼、設定結構、README 都會留著。\n")

    if "--yes" not in sys.argv:
        try:
            answer = input("確定要刪嗎？(y/N) ").strip().lower()
        except EOFError:        # piped or non-interactive: that is not a yes
            answer = ""
        if answer not in ("y", "yes"):
            print("沒有刪任何東西。")
            return 1

    for path, label, _ in found:
        if path == MEDIA:
            # The folder itself stays - the app expects it, and the README tells people to put
            # their images in it. Only what is inside goes.
            for item in MEDIA.iterdir():
                if item.name == ".gitkeep":
                    continue
                shutil.rmtree(item) if item.is_dir() else item.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        print(f"  已刪除 {label}")

    print("\n清乾淨了。新的使用者第一次啟動時會自己建回需要的資料夾，")
    print("然後照 README 第 5 節設定自己的 Facebook。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
