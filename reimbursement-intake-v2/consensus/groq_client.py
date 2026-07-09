"""
Groq LLM client -- a small, dependency-light, never-throws wrapper.

Owner: Subharjun. AgentHack Track 1.

WHY THIS EXISTS
    The four *reasoning* stages of the Consensus Engine are scored by real
    Orchestrator jobs on the UiPath LLM Gateway (that is the "genuinely agentic"
    story, and we keep it). Those jobs are slow -- a full 4-stage debate is ~140s.
    Groq is used for the things that must feel INSTANT and that must never block or
    replace the compliance path:
      * a fast adversarial "red team" voice in the debate  (redteam.py, #4)
      * an instant natural-language "first read" preview while the real jobs run (#5)

    Groq is therefore strictly ADDITIVE and strictly ADVISORY. It can sharpen or
    escalate a verdict (ask for a human), but it can never soften the deterministic
    compliance floor, and if it is unavailable for ANY reason (no key, no network,
    package missing, rate-limited, bad JSON) every call here degrades to `None`
    and the caller falls back to its deterministic path. Nothing here is ever on
    the critical money path.

CONFIG (all env, never hard-coded -- the key must NEVER live in the repo)
    GROQ_API_KEY        required to enable Groq; absent -> every call returns None
    GROQ_MODEL          default "llama-3.3-70b-versatile"
    GROQ_TIMEOUT_S      default 8   (keep it snappy; this is the "fast" path)
    GROQ_ENABLED        set "0" to hard-disable even if a key is present
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

_DEFAULT_MODEL = "llama-3.3-70b-versatile"


def _enabled() -> bool:
    if os.environ.get("GROQ_ENABLED", "").strip() == "0":
        return False
    return bool(os.environ.get("GROQ_API_KEY", "").strip())


def groq_available() -> bool:
    """True iff a Groq call could plausibly succeed (key present + package import).

    Cheap enough to gate UI badges on; does not make a network call.
    """
    if not _enabled():
        return False
    try:
        import groq  # noqa: F401
    except Exception:
        return False
    return True


def _model() -> str:
    return os.environ.get("GROQ_MODEL", "").strip() or _DEFAULT_MODEL


def _timeout() -> float:
    try:
        return float(os.environ.get("GROQ_TIMEOUT_S", "").strip() or 8.0)
    except (TypeError, ValueError):
        return 8.0


def groq_chat(
    system: str,
    user: str,
    *,
    temperature: float = 0.2,
    max_tokens: int = 700,
    model: str | None = None,
) -> str | None:
    """Single-turn chat completion. Returns the assistant text, or None on ANY failure.

    Never raises -- the whole point is that the caller can wrap it in a plain
    `if (txt := groq_chat(...)) is None: <deterministic fallback>`.
    """
    if not _enabled():
        return None
    try:
        from groq import Groq
    except Exception:
        return None
    try:
        client = Groq(
            api_key=os.environ["GROQ_API_KEY"].strip(),
            timeout=_timeout(),
            max_retries=0,
        )
        resp = client.chat.completions.create(
            model=model or _model(),
            temperature=temperature,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return (resp.choices[0].message.content or "").strip() or None
    except Exception:
        # Rate limit, network, auth, quota, malformed response -- all fold to None
        # so the deterministic path takes over. We deliberately swallow the class
        # of error: this is an advisory, off-critical-path helper.
        return None


def groq_json(
    system: str,
    user: str,
    *,
    temperature: float = 0.1,
    max_tokens: int = 700,
    model: str | None = None,
) -> dict[str, Any] | None:
    """Like `groq_chat`, but coerces the reply into a dict. None on any failure.

    Tolerant of models that wrap JSON in prose or ```json fences -- it extracts the
    first balanced `{...}` block. Returns None (not {}) so "no answer" is
    unambiguous to the caller.
    """
    txt = groq_chat(
        system + "\n\nReply with ONLY a single minified JSON object. No prose, no code fences.",
        user,
        temperature=temperature,
        max_tokens=max_tokens,
        model=model,
    )
    if not txt:
        return None
    return _extract_json(txt)


def _extract_json(txt: str) -> dict[str, Any] | None:
    txt = txt.strip()
    # Strip a leading ```json / ``` fence if the model added one.
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", txt, re.DOTALL)
    if fence:
        txt = fence.group(1).strip()
    try:
        obj = json.loads(txt)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    # Last resort: grab the first {...} span and try that.
    start = txt.find("{")
    end = txt.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(txt[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None
