"""
The three agent "voices" of the Consensus Engine, each backed by REAL logic.

Owner: Subharjun. AgentHack Track 1.

This module does NOT re-invent the pipeline's intelligence. It reuses it:

  * FraudSentinel   -> imports `compute_integrity` from FraudIntegrityAgent/detectors.py
                       (the exact duplicate / split-claim / threshold detectors that
                       ship in the deployed agent).
  * Classifier      -> a faithful Python port of the risk-factor rules in
                       ReimbursementClassificationAgent/main.py (`assess_risk`).
  * PolicyArbiter   -> a faithful Python port of the per-category routing rules in
                       PolicyRuleCheckWorkflow/PolicyRuleCheckWorkflow.json (the
                       JavaScript `PolicyEvaluate_1` node).

Keeping the ported rules here (rather than importing the .xaml/.json runtimes,
which can't execute outside Orchestrator) means the debate reasons over the SAME
thresholds the deployed stages use. Every constant below is annotated with its
source so the two never silently drift.
"""

from __future__ import annotations

import os
from datetime import datetime

# Vendored alongside this module (see reimbursement-intake/consensus/) rather
# than imported across repos, so this package has no dependency on the
# AgentHackofUipath monorepo layout.
from .detectors import compute_integrity  # noqa: E402
from .savings import compute_savings      # noqa: E402


# --------------------------------------------------------------------------- #
# Classifier -- ported from ReimbursementClassificationAgent/main.py assess_risk
# --------------------------------------------------------------------------- #
# Thresholds mirror the classifier's `data/mock_policy.json risk_rules`.
_HIGH_AMOUNT_THRESHOLD = float(os.environ.get("RISK_HIGH_AMOUNT", "10000"))
_TRAVEL_MED_MIN = float(os.environ.get("RISK_TRAVEL_MED_MIN", "5000"))
_TRAVEL_MED_MAX = float(os.environ.get("RISK_TRAVEL_MED_MAX", "25000"))
_OCR_MIN = float(os.environ.get("RISK_OCR_MIN", "0.85"))

_EXPENSE_KEYWORDS = {
    "travel": ("uber", "lyft", "flight", "airline", "hotel", "taxi", "cab", "train", "mileage"),
    "food": ("restaurant", "cafe", "coffee", "lunch", "dinner", "pizza", "pita", "meal", "catering"),
    "medical": ("clinic", "hospital", "pharmacy", "doctor", "dental", "medical"),
    "internet": ("internet", "broadband", "wifi", "isp", "telecom", "airtel", "jio"),
    "equipment": ("laptop", "monitor", "keyboard", "hardware", "dell", "apple", "device"),
}


def infer_expense_type(vendor: str | None, purpose: str | None, given: str | None) -> str:
    if given:
        return given
    blob = f"{vendor or ''} {purpose or ''}".lower()
    for etype, kws in _EXPENSE_KEYWORDS.items():
        if any(k in blob for k in kws):
            return etype
    return "others"


def classify(claim: dict) -> dict:
    """Port of the classifier's risk assessment. Returns expense_type, risk_score,
    classification_confidence, business_purpose_valid, and the factors behind them."""
    expense_type = infer_expense_type(
        claim.get("vendor"), claim.get("purpose"), claim.get("expense_type")
    )
    amount = _to_float(claim.get("amount"))
    doc = bool(claim.get("document_attached", True))
    ocr = claim.get("ocr_confidence")
    dup = bool(claim.get("duplicate_detected", False))
    business_purpose_valid = bool(claim.get("business_purpose_valid", True))

    high, medium = [], []
    if expense_type == "medical" and not doc:
        high.append("medical_no_receipt")
    if expense_type == "travel" and amount > _HIGH_AMOUNT_THRESHOLD and not doc:
        high.append("high_travel_no_receipt")
    if expense_type == "others":
        high.append("uncategorised_expense")
    if dup:
        high.append("duplicate_claim_suspected")
    if expense_type == "travel" and _TRAVEL_MED_MIN <= amount <= _TRAVEL_MED_MAX:
        medium.append("travel_medium_band")
    if ocr is not None and _to_float(ocr) < _OCR_MIN:
        medium.append("low_ocr_confidence")
    if not business_purpose_valid:
        high.append("no_business_purpose")

    if high:
        risk = "High"
    elif medium:
        risk = "Medium"
    else:
        risk = "Low"

    # Confidence: clean categorical hit + a receipt -> High; fuzzy/others -> lower.
    if expense_type == "others":
        confidence = "Low"
    elif medium and not high:
        confidence = "Medium"
    else:
        confidence = "High"

    return {
        "expense_type": expense_type,
        "risk_score": risk,
        "classification_confidence": confidence,
        "business_purpose_valid": business_purpose_valid,
        "high_factors": high,
        "medium_factors": medium,
    }


# --------------------------------------------------------------------------- #
# PolicyArbiter -- ported from PolicyRuleCheckWorkflow PolicyEvaluate_1 (JS)
# --------------------------------------------------------------------------- #
_POLICY_DEFAULTS = {
    "travel": {"spend_limit": 50000, "auto_approve_threshold": 5000, "requires_receipt": True, "requires_preapproval_above": 25000},
    "food": {"spend_limit": 2000, "auto_approve_threshold": 2000, "requires_receipt": False, "requires_preapproval_above": 2000},
    "medical": {"spend_limit": 25000, "auto_approve_threshold": 0, "requires_receipt": True, "requires_preapproval_above": 0},
    "internet": {"spend_limit": 2000, "auto_approve_threshold": 2000, "requires_receipt": False, "requires_preapproval_above": 2000},
    "equipment": {"spend_limit": 75000, "auto_approve_threshold": 0, "requires_receipt": True, "requires_preapproval_above": 10000},
    "others": {"spend_limit": 5000, "auto_approve_threshold": 0, "requires_receipt": True, "requires_preapproval_above": 0},
}
_DATE_WINDOW_DAYS = int(os.environ.get("POLICY_DATE_WINDOW_DAYS", "90"))


def policy_check(claim: dict, classified: dict) -> dict:
    """Port of the policy workflow's routing. Uses the classifier's expense_type +
    risk so the two stages agree on the subject under review."""
    et = classified["expense_type"]
    cfg = _POLICY_DEFAULTS.get(et, _POLICY_DEFAULTS["others"])
    amount = _to_float(claim.get("amount"))
    doc = bool(claim.get("document_attached", True))
    bp = classified["business_purpose_valid"]
    risk = classified["risk_score"]
    dup = bool(claim.get("duplicate_detected", False))

    violations: list[str] = []
    within_spend_limit = amount <= cfg["spend_limit"]
    if not within_spend_limit:
        violations.append("over_spend_limit")
    if cfg["requires_receipt"] and not doc:
        violations.append("missing_receipt")

    within_date_window = _within_date_window(claim.get("date"))
    if within_date_window is None:
        violations.append("missing_or_invalid_date")
        within_date_window = False
    elif within_date_window is False:
        violations.append("date_outside_window")

    preapproval_ok = True
    if cfg["requires_preapproval_above"] > 0 and amount > cfg["requires_preapproval_above"]:
        preapproval_ok = False
        violations.append("preapproval_required")
    if not bp:
        violations.append("no_business_purpose")
    if dup:
        violations.append("duplicate_claim")

    if (not within_spend_limit) or dup:
        routing = "reject"
    elif (
        risk == "Low"
        and within_spend_limit
        and amount <= cfg["auto_approve_threshold"]
        and bp
        and (not cfg["requires_receipt"] or doc)
        and within_date_window
        and preapproval_ok
    ):
        routing = "auto_approve"
    else:
        routing = "manager_review"

    return {
        "routing_decision": routing,
        "within_spend_limit": within_spend_limit,
        "within_date_window": bool(within_date_window),
        "preapproval_ok": preapproval_ok,
        "policy_violations": violations,
        "spend_limit": cfg["spend_limit"],
        "auto_approve_threshold": cfg["auto_approve_threshold"],
    }


# --------------------------------------------------------------------------- #
# FraudSentinel -- thin wrapper over the REAL detectors
# --------------------------------------------------------------------------- #
def fraud_screen(claim: dict, history: list[dict] | None = None) -> dict:
    return compute_integrity(claim, history or [])


# --------------------------------------------------------------------------- #
# ROI -- the REAL savings model
# --------------------------------------------------------------------------- #
def roi(claim: dict, duplicate_detected: bool, discount_eligible: bool = False) -> dict:
    return compute_savings(
        amount=_to_float(claim.get("amount")),
        currency=claim.get("currency") or "USD",
        duplicate_detected=duplicate_detected,
        payout_method=claim.get("payout_method", "digital"),
        discount_eligible=discount_eligible,
    )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _to_float(v) -> float:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%m/%d/%y", "%d-%m-%Y", "%Y/%m/%d")


def _within_date_window(v):
    """True / False / None (None = missing or unparseable)."""
    s = str(v or "").strip()
    if not s or s.lower() == "not provided":
        return None
    s = s.split("T")[0].split(" ")[0]
    dt = None
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        return None
    days = (datetime.now() - dt).days
    if days > _DATE_WINDOW_DAYS:
        return False
    if days < -1:
        return False
    return True
