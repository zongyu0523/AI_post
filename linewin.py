"""Driving the LINE desktop app from the outside, without taking your mouse or keyboard.

LINE for Windows is Qt 6 / QML (`Qt6Quick.dll`, `Qt6QuickControls2*.dll`; the Microsoft Store entry
is a sparse MSIX stub that registers this same binary). That rules out two easier routes and leaves
exactly one, all three measured on the real app rather than assumed:

  * **No UIA tree.** Walking it finds 79 elements and *zero* with a name - only Qt's own
    `qt_scrollarea_viewport` / `qt_splithandle_`. QML exposes accessibility per element, opt-in, and
    LINE does not opt in. So nothing can be addressed by name, and text cannot be read back
    through the accessibility API.
  * **No DOM, no CDP.** It is not Electron.
  * **But the Qt event loop accepts posted messages.** A `PostMessage` click on the 群組 tab moved
    22,388 pixels with LINE in the background and the physical cursor untouched, and `WM_CHAR`
    put 測試中文ABC123 into the search box verbatim - no clipboard, no focus steal.

So: `PrintWindow` to see, `PostMessage` to act, template matching to locate. Everything is in the
window's own client coordinates, which is why it does not matter where the window sits or what is
covering it.

The one hard constraint, found the hard way: **a minimized LINE is at (-32000,-32000), 160x28**.
`PrintWindow` then returns a stale strip and clicks land nowhere - and a probe happily reported a
confident verdict off that garbage. `find_window()` refuses it instead.
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

# Report real pixels from GetWindowRect/GetClientRect whatever the display scaling is; the
# PrintWindow bitmap is always physical, and the two have to agree for a click to land.
try:
    ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)   # PER_MONITOR_DPI_AWARE
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0200, 0x0201, 0x0202
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102
MK_LBUTTON = 0x0001
PW_RENDERFULLCONTENT = 0x00000002

VK_BACK, VK_RETURN, VK_ESCAPE, VK_TAB = 0x08, 0x0D, 0x1B, 0x09
VK = {"backspace": VK_BACK, "enter": VK_RETURN, "escape": VK_ESCAPE, "tab": VK_TAB}


class LineNotAvailable(RuntimeError):
    """LINE is not running, or its window cannot be driven right now.

    Always raised before anything could fire, so the gate records `failed` - safe to re-run once
    you fix it.
    """


@dataclass(frozen=True)
class LineWindow:
    hwnd: int
    left: int
    top: int
    width: int
    height: int
    title: str = "LINE"


class _BMIH(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def windows() -> list[LineWindow]:
    """Every drawable LINE window, topmost first.

    LINE puts its side panels in their OWN top-level windows: 記事本 opens as a window titled with
    the GROUP NAME (not "LINE"), as does the member list. Matching on title == "LINE" therefore
    made those panels invisible - clicks that had worked looked like clicks that had done nothing.

    Windows are matched by PROCESS, learned once from the main window, and returned in Z-order
    (EnumWindows enumerates top to bottom), so whatever is in front comes first.
    """
    main_pid = _main_window_pid()
    out: list[LineWindow] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _l):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value != main_pid:
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if "QWindowIcon" not in cls.value:      # skip tooltips, shadows, observer windows
            return True
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        if w < 150 or h < 150 or r.left < -10000:
            return True
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        out.append(LineWindow(hwnd=hwnd, left=r.left, top=r.top, width=w, height=h,
                              title=title.value.strip()))
        return True

    user32.EnumWindows(cb, 0)
    return out


def _process_name(pid: int) -> str:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(512)
        if not k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value.rsplit("\\", 1)[-1].lower()
    finally:
        k32.CloseHandle(handle)


def _main_window_pid() -> int:
    """LINE's process id, found from any of its Qt windows.

    Deliberately NOT "the window titled LINE": the main window can be closed while a panel such as
    記事本 stays open, and that panel is titled with the group name. Keying off the title meant the
    whole channel reported "LINE 沒開" while LINE was plainly running. Other Qt apps are on this
    machine too (an Android emulator among them), so the process executable is what is checked.
    """
    hits: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if "QWindowIcon" not in cls.value:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in hits and _process_name(pid.value) == "line.exe":
            hits.append(pid.value)
        return True

    user32.EnumWindows(cb, 0)
    if not hits:
        raise LineNotAvailable("LINE 沒有在執行")
    return hits[0]


def active_window() -> LineWindow:
    """The window to work on: whatever LINE has in front. Opening 記事本 is therefore followed
    automatically, and closing it falls back to the main window."""
    found = windows()
    if not found:
        raise LineNotAvailable(
            "LINE 視窗不可用 —— 沒開，或被縮到工作列了。還原它就好，被蓋住沒關係。")
    return found[0]


def find_window() -> LineWindow:
    """The LINE MAIN window specifically (readiness checks, "is LINE usable at all")."""
    found: list = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if "QWindowIcon" not in cls.value:
            return True
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        if title.value.strip() != "LINE":
            return True
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        found.append((hwnd, r.left, r.top, r.right - r.left, r.bottom - r.top))
        return True

    user32.EnumWindows(cb, 0)
    if not found:
        raise LineNotAvailable("找不到 LINE 主視窗 — LINE 沒開？")

    hwnd, left, top, w, h = max(found, key=lambda b: b[3] * b[4])
    if user32.IsIconic(hwnd) or left < -10000 or w < 400 or h < 300:
        raise LineNotAvailable(
            f"LINE 視窗被最小化了（{w}x{h} @ ({left},{top})）。"
            "還原它就好 — 被其他視窗蓋住沒關係。")
    return LineWindow(hwnd=hwnd, left=left, top=top, width=w, height=h)


def capture(win: LineWindow):
    """A PIL image of the window, even when it is behind other windows."""
    from PIL import Image

    w, h = win.width, win.height
    hdc_win = user32.GetWindowDC(win.hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    hbm = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    gdi32.SelectObject(hdc_mem, hbm)
    ok = user32.PrintWindow(win.hwnd, hdc_mem, PW_RENDERFULLCONTENT)

    bmi = _BMIH()
    bmi.biSize = ctypes.sizeof(_BMIH)
    bmi.biWidth, bmi.biHeight = w, -h          # negative height = top-down rows
    bmi.biPlanes, bmi.biBitCount, bmi.biCompression = 1, 32, 0
    buf = ctypes.create_string_buffer(w * h * 4)
    lines = gdi32.GetDIBits(hdc_mem, hbm, 0, h, buf, ctypes.byref(bmi), 0)

    gdi32.DeleteObject(hbm)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(win.hwnd, hdc_win)

    if not ok or lines != h:
        raise LineNotAvailable(f"抓不到 LINE 畫面（PrintWindow={ok}, scanlines={lines}）")
    return Image.frombuffer("RGBA", (w, h), bytes(buf), "raw", "BGRA", 0, 1).convert("RGB")


def _lparam(x: int, y: int) -> int:
    return (y << 16) | (x & 0xFFFF)


HOVER = 0.30          # measured: a press sent sooner than this is ignored outright
PRESS_HOLD = 0.09


def _press(win: "LineWindow", x: int, y: int) -> None:
    user32.PostMessageW(win.hwnd, WM_LBUTTONDOWN, MK_LBUTTON, _lparam(x, y))
    time.sleep(PRESS_HOLD)
    user32.PostMessageW(win.hwnd, WM_LBUTTONUP, 0, _lparam(x, y))


def click_once(win: "LineWindow", x: int, y: int, *, settle: float = 0.9) -> None:
    """Exactly one press. Used where a second press would be a second irreversible action."""
    user32.PostMessageW(win.hwnd, WM_MOUSEMOVE, 0, _lparam(x, y))
    time.sleep(HOVER)
    _press(win, x, y)
    time.sleep(settle)


def _top_level_handles() -> set[int]:
    """Every visible top-level window of LINE's process, whatever its class.

    Deliberately broader than `windows()`: a click can land by opening something that is not a Qt
    window at all - the Windows file picker is a plain `#32770` - and a click that opened a dialog
    must not be judged as a click that did nothing.
    """
    try:
        pid = _main_window_pid()
    except LineNotAvailable:
        return set()
    out: set[int] = set()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid:
            return True
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        if r.right - r.left > 150 and r.bottom - r.top > 120:
            out.add(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return out


def click(win: "LineWindow", x: int, y: int, *, settle: float = 0.9,
          tolerance: int = 400) -> bool:
    """Click, look, and press again only if nothing happened. Returns whether anything changed.

    Some of LINE's QML controls - the chat toolbar icons among them - ignore a single posted press
    and need a second one at the same point (see the module header for the measurements). Checking
    in between means a press that already worked is never repeated, which matters because the same
    primitive is used on buttons that cannot be pressed twice.

    "Nothing happened" means BOTH that this window is unchanged AND that no new window appeared.
    Without the second half, clicking a control that opens a dialog - the photo button opens the
    Windows file picker, and the picker is a window of its own - looks like a click that missed,
    and the retry closes the thing that just opened.
    """
    before = capture(win)
    before_windows = _top_level_handles()
    user32.PostMessageW(win.hwnd, WM_MOUSEMOVE, 0, _lparam(x, y))
    time.sleep(HOVER)
    _press(win, x, y)
    time.sleep(settle)

    def landed() -> bool:
        if _top_level_handles() - before_windows:
            return True
        try:
            return region_changed(before, capture(win),
                                  (0, 0, win.width, win.height)) > tolerance
        except Exception:
            return True        # cannot tell - assume it landed rather than press again

    if landed():
        return True
    _press(win, x, y)          # nothing moved and nothing opened, so the press did nothing
    time.sleep(settle)
    return landed()


def type_text(win: LineWindow, text: str, *, settle: float = 0.5) -> None:
    """Type into whatever Qt currently focuses. WM_CHAR carries a UTF-16 code unit, so Chinese goes
    straight in - no clipboard to clobber, no window to activate."""
    for ch in text:
        raw = ch.encode("utf-16-le")           # 1 unit for BMP, 2 for a surrogate pair
        for i in range(0, len(raw), 2):
            user32.PostMessageW(win.hwnd, WM_CHAR, raw[i] | (raw[i + 1] << 8), 1)
        time.sleep(0.012)
    time.sleep(settle)


def press(win: LineWindow, key: str, times: int = 1, *, settle: float = 0.3) -> None:
    code = VK.get(key.lower())
    if code is None:
        raise ValueError(f"不支援的按鍵: {key}")
    for _ in range(times):
        user32.PostMessageW(win.hwnd, WM_KEYDOWN, code, 1)
        user32.PostMessageW(win.hwnd, WM_KEYUP, code, 1)
        time.sleep(0.03)
    time.sleep(settle)


# ───────────────────────────────────────────────────────────────────── locating things
def locate(haystack, template_path: Path, threshold: float = 0.87) -> tuple[int, int, float]:
    """Find `template_path` in a captured window image. Returns (center_x, center_y, score).

    Template matching rather than fixed coordinates because the window moves and resizes; the
    template is matched against the window's own bitmap, so screen position is irrelevant.
    """
    import cv2
    import numpy as np

    tpl = cv2.imread(str(template_path), cv2.IMREAD_COLOR)
    if tpl is None:
        raise LineNotAvailable(f"讀不到模板圖: {template_path.name}")
    hay = cv2.cvtColor(np.array(haystack), cv2.COLOR_RGB2BGR)
    if tpl.shape[0] > hay.shape[0] or tpl.shape[1] > hay.shape[1]:
        raise LineNotAvailable(f"模板比視窗還大: {template_path.name}")

    res = cv2.matchTemplate(hay, tpl, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)
    if score < threshold:
        raise LineNotAvailable(
            f"畫面上找不到「{template_path.stem}」(最佳相似度 {score:.2f} < {threshold})")
    return loc[0] + tpl.shape[1] // 2, loc[1] + tpl.shape[0] // 2, float(score)


def region_changed(before, after, box: tuple[int, int, int, int], tolerance: int = 10) -> int:
    """Pixels differing inside `box`. The only read-back this app has - there is no text to query,
    so 'did the note appear' is answered by how much of that region redrew."""
    import numpy as np

    a = np.asarray(before.crop(box), dtype=np.int16)
    b = np.asarray(after.crop(box), dtype=np.int16)
    if a.shape != b.shape:
        return a.size
    return int((np.abs(a - b).max(axis=2) > tolerance).sum())
