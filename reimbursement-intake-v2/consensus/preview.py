"""
Fast preview -- an instant "first read" while the real jobs run (#5).

Owner: Subharjun. AgentHack Track 1.

WHY THIS EXISTS
    A full live debate scores four stages as REAL Orchestrator jobs and takes
    ~140s. That is the right thing for the record of decision, but it means the
    dashboard has nothing to show for over two minutes after a submission. This
    module fills that gap: it computes the SAME debate in all-local deterministic
    mode (sub-second, no network) to get a genuine, defensible verdict RIGHT NOW,
    then uses Groq to phrase it as a one-line human "first read".

    Crucially, Groq here is only allowed to *phrase* -- it is told the deterministic
    verdict and its reason and asked to restate it crisply; it may not change or
    second-guess the verdict and it may not add facts. So the instant read can
    never contradict the compliance path, and if Groq is unavailable we fall back
    to the deterministic rationale verbatim. The full live debate that lands ~140s
    later remains the authoritative record; this is explicitly a preview.
"""

from __future__ import annotations

import time

from .debate import run_debate
from .groq_client import groq_chat, groq_available
from .agents import _to_float


def _clean_one_line(txt: str, limit: int = 220) -> str:
    line = " ".join((txt or "").split()).strip().strip('"').strip()
    return line[:limit].rstrip()


def fast_preview(claim: dict, history: list[dict] | None = None) -> dict:
    """Instant, sub-second first read for a claim. Never raises.

    Returns:
      { recommendation, path, headline, headline_source ("groq"|"deterministic"),
        top_reason, redteam {argument, pushes_to, focus, source},
        elapsed_ms, basis, is_preview: True }
    """
    t0 = time.perf_counter()
    history = history or []

    # Real verdict, computed locally (deterministic + heuristic red team) so it is
    # instant and needs no network. This IS a genuine debate result, just not the
    # live-job one.
    try:
        local = run_debate(claim, history, live=False)
    except Exception as exc:  # never let a preview break the caller
        return {
            "recommendation": None, "path": None,
            "headline": "First read unavailable — the full debate is still running.",
            "headline_source": "deterministic", "top_reason": str(exc),
            "redteam": None, "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "basis": "preview failed; live debate is authoritative", "is_preview": True,
        }

    rec = local["recommendation"]
    reason = local["rationale"]
    amt = _to_float(claim.get("amount"))
    cur = claim.get("currency") or "USD"
    vendor = claim.get("vendor") or "(unknown vendor)"

    headline = reason
    headline_source = "deterministic"
    if groq_available():
        system = (
            "You write a single one-line 'first read' for a busy expense reviewer. "
            "You are given the system's verdict and the reason behind it. Restate it "
            "in ONE crisp sentence of at most 25 words that a reviewer can act on. "
            "You must NOT change, soften, or second-guess the verdict, and you must "
            "NOT introduce any fact you were not given. Plain text only."
        )
        user = (
            f"Verdict: {rec}. Reason: {reason}. "
            f"Claim: {amt:,.2f} {cur} to '{vendor}'."
        )
        txt = groq_chat(system, user, temperature=0.2, max_tokens=90)
        if txt:
            headline = _clean_one_line(txt)
            headline_source = "groq"

    rt = local.get("redteam") or {}
    return {
        "recommendation": rec,
        "path": local["path"],
        "headline": headline,
        "headline_source": headline_source,
        "top_reason": reason,
        "redteam": {
            "argument": rt.get("argument"),
            "pushes_to": rt.get("pushes_to"),
            "focus": rt.get("focus"),
            "source": rt.get("source"),
        },
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "basis": "instant local-deterministic read; the full 4-agent live debate follows",
        "is_preview": True,
    }
