"""Channel registry.

`describe()` is what the UI renders. It reports three different things on purpose, because
collapsing them is how a tool ends up denying a capability it has: a channel can *exist* in this
build, be *supported* (there is an adapter), and be *ready* (its config is in place right now).
An earlier project made that mistake - "can I
run it" answered as "does it exist".
"""
from __future__ import annotations

from .base import Channel, ChannelNotReady
from .facebook_page import FacebookPage
from .line_note import LineGroupNote

REGISTRY: dict[str, Channel] = {c.key: c for c in (FacebookPage(), LineGroupNote())}


class UnknownChannel(LookupError):
    """A channel key that is not in this build. Its own type so the API can answer 400 rather than
    letting a typo surface as a 500."""


def get(key: str) -> Channel:
    try:
        return REGISTRY[key]
    except KeyError:
        raise UnknownChannel(
            f"沒有這個頻道: {key}（有的是: {', '.join(REGISTRY)}）") from None


def describe() -> list[dict]:
    out = []
    for ch in REGISTRY.values():
        try:
            ready, reason = ch.readiness()
        except Exception as exc:                      # readiness must never break the UI
            ready, reason = False, f"檢查失敗: {type(exc).__name__}: {exc}"
        out.append({"key": ch.key, "label": ch.label, "runtime": ch.runtime,
                    "hint": ch.hint, "ready": ready, "reason": reason})
    return out


__all__ = ["Channel", "ChannelNotReady", "UnknownChannel", "REGISTRY", "get", "describe"]
