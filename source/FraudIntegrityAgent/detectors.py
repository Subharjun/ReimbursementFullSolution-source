"""
Deterministic reimbursement-fraud / integrity detectors.

Owner: Subharjun. AgentHack Track 1.

This module is the "code decides" half of the FraudIntegrityAgent. It contains
ZERO LLM calls and ZERO I/O -- it is a pure function over the current claim plus
an optional list of the claimant's recent prior claims (`history`). The LLM in
`main.py` only ever *explains* the flags this module produces; it never invents a
verdict. That separation is deliberate: fraud decisions must be reproducible,
auditable, and defensible ("not a black box"), so every signal here is a plain
rule with a transparent threshold, and the thresholds themselves are returned in
the `assumptions` block and are tunable via `FRAUD_*` environment variables.

What it catches that a naive invoice reader does not:
  - DUPLICATE      same receipt / same vendor+amount resubmitted (double payment)
  - SPLIT_CLAIM    one expense broken into several sub-threshold claims to dodge
                   the approval gate (classic reimbursement fraud)
  - THRESHOLD_HUG  a single amount parked just under the approval limit
  - VELOCITY       an implausible burst of claims in a short window
  - ROUND_AMOUNT   suspiciously round figures (weak, contextual signal)
  - WEEKEND_DATE   expense dated on a weekend (weak, contextual signal)

The duplicate/split signals are the ones that make the pipeline's ROI real: the
NotificationAgent's savings model credits "duplicate double-payment prevented",
and THIS is the component that actually earns that credit instead of assuming it.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from typing import Any


# --------------------------------------------------------------------------- #
# Tunable thresholds (env-overridable; defaults are sensible AP norms)
# --------------------------------------------------------------------------- #
def _num_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


# The approval limit a claim must clear without extra scrutiny (in claim currency
# units -- we treat the number nominally; a real deployment would normalise FX).
APPROVAL_THRESHOLD = _num_env("FRAUD_APPROVAL_THRESHOLD", 500.0)
# How far below the threshold still counts as "hugging" it.
THRESHOLD_HUG_PCT = _num_env("FRAUD_THRESHOLD_HUG_PCT", 0.08)  # within 8%
# Duplicate: same vendor+amount seen again within this many days.
DUP_WINDOW_DAYS = int(_num_env("FRAUD_DUP_WINDOW_DAYS", 45))
# Split: several sub-threshold claims to the same vendor inside this window that
# together clear the threshold.
SPLIT_WINDOW_DAYS = int(_num_env("FRAUD_SPLIT_WINDOW_DAYS", 7))
SPLIT_MIN_PARTS = int(_num_env("FRAUD_SPLIT_MIN_PARTS", 2))
# Velocity: more than this many claims from the same person inside the window.
VELOCITY_WINDOW_DAYS = int(_num_env("FRAUD_VELOCITY_WINDOW_DAYS", 3))
VELOCITY_MAX = int(_num_env("FRAUD_VELOCITY_MAX", 4))
# Round-amount trigger: integer amount that is a clean multiple of this and large.
ROUND_MULTIPLE = _num_env("FRAUD_ROUND_MULTIPLE", 50.0)
ROUND_MIN = _num_env("FRAUD_ROUND_MIN", 100.0)

# Per-flag contribution to the 0-100 fraud score (severity weight).
_WEIGHTS = {
    "DUPLICATE": 70,
    "SPLIT_CLAIM": 55,
    "THRESHOLD_HUG": 25,
    "VELOCITY": 25,
    "ROUND_AMOUNT": 8,
    "WEEKEND_DATE": 5,
}
# Score -> risk band cutoffs.
HIGH_AT = int(_num_env("FRAUD_HIGH_AT", 60))
MEDIUM_AT = int(_num_env("FRAUD_MEDIUM_AT", 25))


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def _to_float(v: Any) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(v or ""))
    if not m:
        return 0.0
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return 0.0


_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%m/%d/%y", "%d-%m-%Y", "%Y/%m/%d")


def _to_date(v: Any) -> datetime | None:
    s = str(v or "").strip()
    if not s:
        return None
    s = s.split("T")[0].split(" ")[0]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _norm_vendor(v: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(v or "").lower())


def _days_between(a: datetime | None, b: datetime | None) -> int | None:
    if a is None or b is None:
        return None
    return abs((a - b).days)


# --------------------------------------------------------------------------- #
# The detectors
# --------------------------------------------------------------------------- #
def _flag(code: str, severity: str, title: str, detail: str) -> dict:
    return {"code": code, "severity": severity, "title": title, "detail": detail}


def compute_integrity(claim: dict, history: list[dict] | None = None) -> dict:
    """Run every detector over `claim` (+ optional `history`) and return a verdict.

    `claim`   : {vendor, amount, currency, date, employee_email, case_id, ...}
    `history` : list of the SAME shape -- the claimant's recent prior claims.

    Returns a dict with fraud_score (0-100), integrity_risk, recommendation,
    duplicate_detected, split_claim_detected, flags[], and the assumptions used.
    """
    history = history or []
    flags: list[dict] = []

    cur_vendor = _norm_vendor(claim.get("vendor"))
    cur_amount = _to_float(claim.get("amount"))
    cur_date = _to_date(claim.get("date"))
    cur_email = str(claim.get("employee_email") or "").strip().lower()

    duplicate_detected = False
    split_claim_detected = False

    # 1) DUPLICATE -- same vendor + same amount seen recently in history.
    for h in history:
        if _norm_vendor(h.get("vendor")) != cur_vendor or not cur_vendor:
            continue
        if abs(_to_float(h.get("amount")) - cur_amount) > 0.005:
            continue
        h_date = _to_date(h.get("date"))
        gap = _days_between(cur_date, h_date)
        same_day = gap == 0 or (cur_date is None or h_date is None)
        if same_day or (gap is not None and gap <= DUP_WINDOW_DAYS):
            duplicate_detected = True
            ref = h.get("case_id") or "a prior claim"
            when = f"{gap} day(s) apart" if gap else "on the same date"
            flags.append(
                _flag(
                    "DUPLICATE",
                    "high",
                    "Possible duplicate claim",
                    f"An earlier claim ({ref}) to '{claim.get('vendor')}' for the same "
                    f"amount was found {when} -- risk of paying the same expense twice.",
                )
            )
            break

    # 2) SPLIT_CLAIM -- several sub-threshold claims to one vendor in a tight
    #    window that together clear the approval limit.
    if cur_vendor and cur_amount < APPROVAL_THRESHOLD:
        cluster = [claim] + [
            h
            for h in history
            if _norm_vendor(h.get("vendor")) == cur_vendor
            and _to_float(h.get("amount")) < APPROVAL_THRESHOLD
            and (
                cur_date is None
                or _to_date(h.get("date")) is None
                or (_days_between(cur_date, _to_date(h.get("date"))) or 999) <= SPLIT_WINDOW_DAYS
            )
        ]
        total = sum(_to_float(c.get("amount")) for c in cluster)
        if len(cluster) >= SPLIT_MIN_PARTS and total >= APPROVAL_THRESHOLD:
            split_claim_detected = True
            flags.append(
                _flag(
                    "SPLIT_CLAIM",
                    "high",
                    "Possible split claim (threshold avoidance)",
                    f"{len(cluster)} claims to '{claim.get('vendor')}' within "
                    f"{SPLIT_WINDOW_DAYS} days each sit below the {APPROVAL_THRESHOLD:g} "
                    f"approval limit but total {total:,.2f} -- consistent with splitting "
                    "one expense to avoid approval.",
                )
            )

    # 3) THRESHOLD_HUG -- a lone amount parked just under the limit.
    lo = APPROVAL_THRESHOLD * (1 - THRESHOLD_HUG_PCT)
    if lo <= cur_amount < APPROVAL_THRESHOLD and not split_claim_detected:
        flags.append(
            _flag(
                "THRESHOLD_HUG",
                "medium",
                "Amount just under the approval limit",
                f"{cur_amount:,.2f} sits within {THRESHOLD_HUG_PCT:.0%} of the "
                f"{APPROVAL_THRESHOLD:g} approval threshold -- worth a glance.",
            )
        )

    # 4) VELOCITY -- burst of claims from the same person.
    if cur_email:
        recent = [
            h
            for h in history
            if str(h.get("employee_email") or "").strip().lower() == cur_email
            and (
                cur_date is None
                or _to_date(h.get("date")) is None
                or (_days_between(cur_date, _to_date(h.get("date"))) or 999) <= VELOCITY_WINDOW_DAYS
            )
        ]
        if len(recent) + 1 > VELOCITY_MAX:
            flags.append(
                _flag(
                    "VELOCITY",
                    "medium",
                    "Unusual claim frequency",
                    f"{len(recent) + 1} claims from this employee within "
                    f"{VELOCITY_WINDOW_DAYS} days exceeds the expected {VELOCITY_MAX}.",
                )
            )

    # 5) ROUND_AMOUNT -- weak, contextual.
    if cur_amount >= ROUND_MIN and abs(cur_amount % ROUND_MULTIPLE) < 0.005:
        flags.append(
            _flag(
                "ROUND_AMOUNT",
                "low",
                "Suspiciously round amount",
                f"{cur_amount:,.2f} is an exact multiple of {ROUND_MULTIPLE:g}; genuine "
                "receipts rarely land on round figures.",
            )
        )

    # 6) WEEKEND_DATE -- weak, contextual.
    if cur_date is not None and cur_date.weekday() >= 5:
        flags.append(
            _flag(
                "WEEKEND_DATE",
                "low",
                "Weekend expense date",
                f"The expense is dated {cur_date.strftime('%A, %d %b %Y')} (a weekend) -- "
                "context only, not itself a problem.",
            )
        )

    # Score + band.
    score = min(100, sum(_WEIGHTS.get(f["code"], 0) for f in flags))
    if score >= HIGH_AT:
        risk, recommendation = "High", "reject"
    elif score >= MEDIUM_AT:
        risk, recommendation = "Medium", "review"
    else:
        risk, recommendation = "Low", "proceed"

    return {
        "fraud_score": score,
        "integrity_risk": risk,
        "recommendation": recommendation,
        "duplicate_detected": duplicate_detected,
        "split_claim_detected": split_claim_detected,
        "flags": flags,
        "assumptions": {
            "approval_threshold": APPROVAL_THRESHOLD,
            "threshold_hug_pct": THRESHOLD_HUG_PCT,
            "duplicate_window_days": DUP_WINDOW_DAYS,
            "split_window_days": SPLIT_WINDOW_DAYS,
            "velocity_window_days": VELOCITY_WINDOW_DAYS,
            "velocity_max": VELOCITY_MAX,
            "high_at": HIGH_AT,
            "medium_at": MEDIUM_AT,
            "history_size": len(history),
        },
    }


def summary_line(result: dict) -> str:
    """One-line plain-text summary for logs / quick display."""
    codes = ", ".join(f["code"] for f in result.get("flags", [])) or "no flags"
    return (
        f"Integrity: {result.get('integrity_risk')} "
        f"(score {result.get('fraud_score')}/100, {result.get('recommendation')}) "
        f"[{codes}]"
    )
