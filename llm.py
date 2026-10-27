"""One OpenAI-compatible chat client, so the endpoint is a setting rather than a rewrite.

MiniMax, OpenRouter, and an Anthropic-style relay all speak this shape, so `base_url` / `api_key` /
`model` live in config and nothing else in the project knows which vendor is behind them.

The endpoint and model are pinned to a known-good pair, so the only thing anyone has
to supply is the key. Both are still overridable by environment variable for the day that changes,
but neither is a question the UI asks.

    AIPOST_API_KEY      the only setting that matters (or paste it in the UI)
    AIPOST_API_BASE     override the pinned endpoint
    AIPOST_MODEL        override the pinned model
    AIPOST_VISION_MODEL override the model used for the LINE template rescue
"""
from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

import store

TIMEOUT = httpx.Timeout(120.0, connect=20.0)

# Pinned, not configured: two fewer things for a non-technical user to get wrong.
DEFAULT_BASE_URL = "https://api.minimax.io/v1"
DEFAULT_MODEL = "MiniMax-M3.1-Flash"


class LLMError(RuntimeError):
    """Anything that stops us getting a usable reply. Always raised before an act, so a channel
    that hits it lands `failed` (provably nothing published)."""


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    vision_model: str

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def masked(self) -> dict:
        tail = self.api_key[-4:] if len(self.api_key) > 8 else ""
        return {"base_url": self.base_url, "model": self.model,
                "vision_model": self.vision_model,
                "api_key": f"····{tail}" if self.api_key else "",
                "configured": self.configured}


def _pick(env_name: str, setting: str) -> str:
    return (os.getenv(env_name) or store.get_setting(setting, "")).strip()


def load_config() -> LLMConfig:
    model = _pick("AIPOST_MODEL", "llm_model") or DEFAULT_MODEL
    return LLMConfig(
        base_url=(_pick("AIPOST_API_BASE", "llm_base_url") or DEFAULT_BASE_URL).rstrip("/"),
        api_key=_pick("AIPOST_API_KEY", "llm_api_key"),
        model=model,
        vision_model=_pick("AIPOST_VISION_MODEL", "llm_vision_model") or model,
    )


async def chat(cfg: LLMConfig, messages: list[dict], *, tools: list[dict] | None = None,
               model: str | None = None, max_tokens: int = 1200,
               temperature: float = 0.0) -> dict:
    """One completion. Returns the assistant message dict (may carry `tool_calls`)."""
    if not cfg.configured:
        raise LLMError("還沒填 API key")

    payload: dict = {
        "model": model or cfg.model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                f"{cfg.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {cfg.api_key}",
                         "Content-Type": "application/json"},
                json=payload)
    except httpx.HTTPError as exc:
        raise LLMError(f"連不上 LLM: {type(exc).__name__}: {exc}") from exc

    if resp.status_code != 200:
        raise LLMError(f"LLM 回 {resp.status_code}: {resp.text[:300]}")
    try:
        body = resp.json()
        return body["choices"][0]["message"]
    except Exception as exc:
        raise LLMError(f"看不懂 LLM 的回覆: {resp.text[:300]}") from exc


_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


def strip_think(text: str) -> str:
    """Drop a reasoning model's visible scratchpad.

    MiniMax-M3 returns its chain of thought inline as `<think>...</think>` in `content`. It is not
    an answer, it should never reach the UI, and a greedy JSON scrape of a reply would otherwise
    start inside it. Tool calls are unaffected - they arrive in `tool_calls`, not here.
    """
    return _THINK.sub("", text or "").strip()


def image_part(path: Path | str, detail: str = "high") -> dict:
    """A PNG as an OpenAI-style image content part."""
    raw = Path(path).read_bytes()
    uri = "data:image/png;base64," + base64.b64encode(raw).decode()
    return {"type": "image_url", "image_url": {"url": uri, "detail": detail}}


async def probe(cfg: LLMConfig) -> str:
    """Settings-page connectivity test. Returns the model's reply, or raises LLMError.

    The budget is generous because a reasoning model spends most of it thinking: at 16 tokens
    MiniMax-M3 returned nothing but an unterminated `<think>`.
    """
    msg = await chat(cfg, [{"role": "user", "content": "reply with the single word: ok"}],
                     max_tokens=512)
    return strip_think(msg.get("content") or "")[:80] or "(只有思考，沒有內容)"
