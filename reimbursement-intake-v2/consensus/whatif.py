"""
What-if verdict simulator -- re-runs the deterministic consensus core on a
hypothetical claim and finds the exact decision boundaries.

Owner: Subharjun. AgentHack Track 1.

WHY THIS EXISTS
    The debate explains ONE claim. The simulator explains the RULE SURFACE: drag
    the amount slider on /dashboard and watch the committee's verdict flip in
    real time, with the exact rupee where it flips ("at 5,001 INR this leaves
    auto-approve and goes to a human"). It runs the SAME deterministic ports the
    live agents floor their LLM judgments on (agents.py / detectors.py /
    debate._arbitrate), so the boundaries shown are the real compliance floor --
    not a lookup table that could drift from the code.

    Purely observational: nothing here starts jobs, sends email, or touches the
    Case. It's the offline consensus core called in a loop.
"""

from __future__ import annotations

import math

from .agents import classify, fraud_screen, policy_check, _to_float
from .debate import _arbitrate, _family


def _verdict(claim: dict, history: list[dict] | None = None) -> dict:
    """One deterministic pass: classify -> fraud -> policy -> arbitrate."""
    cls = classify(claim)
    frd = fraud_screen(claim, history or [])
    pol = policy_check(claim, cls)
    recommendation, path, rationale = _arbitrate(cls, frd, pol)
    return {
        "recommendation": recommendation, "path": path, "rationale": rationale,
        "family": _family(recommendation),
        "expense_type": cls["expense_type"],
        "risk_score": cls["risk_score"],
        "fraud_score": frd["fraud_score"],
        "duplicate_detected": frd["duplicate_detected"],
        "policy_violations": pol["policy_violations"],
        "spend_limit": pol["spend_limit"],
        "auto_approve_threshold": pol["auto_approve_threshold"],
    }


def _flip_point(claim: dict, lo: float, hi: float, history) -> float | None:
    """Smallest amount in (lo, hi] where the verdict family differs from at lo,
    found by bisection (the deterministic core is monotone in amount for a fixed
    claim, so bisection is exact to the rupee)."""
    base = _verdict({**claim, "amount": lo}, history)["family"]
    if _verdict({**claim, "amount": hi}, history)["family"] == base:
        return None
    for _ in range(40):
        mid = (lo + hi) / 2
        if _verdict({**claim, "amount": mid}, history)["family"] == base:
            lo = mid
        else:
            hi = mid
        if hi - lo <= 0.5:
            break
    # `hi` is always on the NEW-family side of the bracket; round UP so the
    # returned amount is the first integer that actually flips (rounding down
    # would land back on the base-family side and hide the boundary).
    return math.ceil(hi)


def simulate(claim: dict, history: list[dict] | None = None) -> dict:
    """Verdict for the hypothetical claim + every amount boundary on its rule
    surface (where the outcome family changes as amount grows)."""
    history = history or []
    amt = _to_float(claim.get("amount"))
    now = _verdict(claim, history)

    # Sweep the amount axis for family flips: probe band edges, bisect each.
    limit = max(now["spend_limit"], amt, 1.0)
    probes = sorted({p for p in (1.0, now["auto_approve_threshold"],
                                 now["spend_limit"], limit * 1.5, amt) if p >= 1.0})
    boundaries: list[dict] = []
    lo = probes[0]
    lo_fam = _verdict({**claim, "amount": lo}, history)["family"]
    for hi in probes[1:]:
        if hi <= lo:
            continue
        flip = _flip_point(claim, lo, hi, history)
        if flip is not None:
            after = _verdict({**claim, "amount": flip}, history)
            if after["family"] != lo_fam:
                boundaries.append({
                    "at_amount": flip,
                    "from_family": lo_fam,
                    "to_family": after["family"],
                    "to_recommendation": after["recommendation"],
                })
                lo_fam = after["family"]
        lo = hi

    # Distance from the submitted amount to the next boundary above/below it.
    above = next((b for b in boundaries if b["at_amount"] > amt), None)
    below = next((b for b in reversed(boundaries) if b["at_amount"] <= amt), None)
    return {
        "verdict": now,
        "boundaries": boundaries,
        "next_boundary_above": above,
        "last_boundary_below": below,
        "headroom": (above["at_amount"] - amt) if above else None,
        "generated_by": (
            "Deterministic consensus core (the same compliance floor the live "
            "LLM agents are clamped to) re-run across the amount axis. "
            "Observational only — no jobs started, nothing dispatched."
        ),
    }
