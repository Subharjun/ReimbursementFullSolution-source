"""
AgentHack Track 1 - Stage 3: Reimbursement Classification External Agent.

Owner: Subharjun.

Graph:  START -> classify (LLM + deterministic fallback) -> score_risk (deterministic) -> END

- classify    : reads the free-text email + IDP fields, returns expense_type,
                business_purpose_valid, classification_confidence.
                Uses UiPathChat (gpt-4o, temperature 0) via the UiPath LLM Gateway,
                with a deterministic keyword fallback so the agent still runs
                locally without auth / Agent Units.
- score_risk  : pure-Python implementation of the Low/Medium/High risk rules
                + duplicate override, builds risk_factors and audit_entry.
                Terminal node -> emits the full Stage 3 output contract.

All thresholds and keywords come from data/mock_policy.json (the team's
single source of truth). Hand-off contract goes to Mir's Stage 4.
"""

import json
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

ExpenseType = Literal["travel", "food", "medical", "internet", "equipment", "others"]
RiskScore = Literal["Low", "Medium", "High"]
Confidence = Literal["High", "Medium", "Low"]

POLICY_PATH = Path(__file__).parent / "data" / "mock_policy.json"


@lru_cache(maxsize=1)
def load_policy() -> dict:
    """Load the mock policy DB once. Plain JSON read - safe at import/init."""
    with open(POLICY_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ----------------------------------------------------------------------------
# Schemas
# ----------------------------------------------------------------------------
class GraphInput(BaseModel):
    case_id: str = Field(description="Maestro case ID from Stage 1")
    source_email_body: str = Field(default="", description="Raw email body from Stage 1")
    reason: Optional[str] = Field(default=None, description="Stated business reason/justification (IDP/intake); used for purpose validation")
    vendor: Optional[str] = Field(default=None, description="Vendor extracted by Rashmi IDP")
    date: Optional[str] = Field(default=None, description="YYYY-MM-DD, may be null")
    amount: float = Field(default=0, description="Amount extracted by IDP, may be 0")
    currency: str = Field(default="INR", description="Currency, default INR")
    document_attached: bool = Field(default=False, description="IDP confidence > 0.85")
    ocr_confidence: Optional[float] = Field(default=None, description="IDP OCR confidence 0..1")
    employee_email: str = Field(default="", description="Sender email from Stage 1")
    employee_name: str = Field(default="", description="Employee name from Stage 1; derived from the email local-part if empty")
    duplicate_detected: bool = Field(
        default=False,
        description="Set by the upstream Orchestrator duplicate check; the agent honours it as a risk override.",
    )


class GraphOutput(BaseModel):
    # ---- pass-through fields (so this output is a drop-in for Mir's Stage 4 input) ----
    case_id: str
    expense_type: ExpenseType
    amount: float
    currency: str
    vendor: str
    date: str
    reason: str
    document_attached: bool
    # ---- classification analysis (produced by this agent) ----
    risk_score: RiskScore
    duplicate_detected: bool
    business_purpose_valid: bool
    # ---- claimant identity (carried through for notify / audit; Mir ignores these) ----
    employee_name: str
    employee_email: str
    # ---- extras for audit / debugging (Mir ignores these) ----
    risk_factors: list[str]
    classification_confidence: Confidence
    audit_entry: dict


class GraphState(BaseModel):
    # inputs
    case_id: str
    source_email_body: str = ""
    reason: Optional[str] = None
    vendor: Optional[str] = None
    date: Optional[str] = None
    amount: float = 0
    currency: str = "INR"
    document_attached: bool = False
    ocr_confidence: Optional[float] = None
    employee_email: str = ""
    employee_name: str = ""
    duplicate_detected: bool = False
    # produced by classify node
    expense_type: Optional[ExpenseType] = None
    business_purpose_valid: Optional[bool] = None
    classification_confidence: Confidence = "High"


class _LLMClassification(BaseModel):
    """Structured-output shape requested from the LLM."""

    expense_type: ExpenseType
    business_purpose_valid: bool
    classification_confidence: Confidence


# ----------------------------------------------------------------------------
# Deterministic helpers (also the fallback when the LLM is unavailable)
# ----------------------------------------------------------------------------
def classify_expense_type_keywords(text: str, vendor: Optional[str]) -> ExpenseType:
    policy = load_policy()
    haystack = f"{text or ''} {vendor or ''}".lower()
    for etype, keywords in policy["expense_type_keywords"].items():
        if any(kw in haystack for kw in keywords):
            return etype  # type: ignore[return-value]
    return "others"


def derive_employee_name(employee_name: str, employee_email: str) -> str:
    """Return a human-readable claimant name.

    Stage 1 intake may not supply a name; fall back to the email local-part
    (e.g. "subharjun.bose28@gmail.com" -> "Subharjun Bose") so the Stage 3
    output (and downstream Notify) always has a name to address.
    """
    if employee_name and employee_name.strip():
        return employee_name.strip()
    local = (employee_email or "").split("@", 1)[0]
    # Drop trailing digits on the local-part (e.g. "bose28" -> "bose").
    parts = [re.sub(r"\d+$", "", p) for p in re.split(r"[._\-]+", local) if p]
    name = " ".join(p.capitalize() for p in parts if p)
    return name or "not provided"


def validate_business_purpose_keywords(text: str) -> bool:
    policy = load_policy()
    bp = policy["business_purpose_keywords"]
    body = (text or "").lower()
    if not body.strip():
        return False
    if any(kw in body for kw in bp["invalid"]):
        return False
    return any(kw in body for kw in bp["valid"])


# Maps a currency symbol / code / word (lowercased) to a supported ISO code.
_CURRENCY_MAP = {
    "₹": "INR", "rs": "INR", "rs.": "INR", "inr": "INR", "rupee": "INR", "rupees": "INR",
    "$": "USD", "usd": "USD", "dollar": "USD", "dollars": "USD",
    "€": "EUR", "eur": "EUR", "euro": "EUR", "euros": "EUR",
    "£": "GBP", "gbp": "GBP", "pound": "GBP", "pounds": "GBP",
    "aed": "AED", "dirham": "AED", "dirhams": "AED",
}

# Matches a number with an optional leading symbol and/or leading/trailing currency token.
_AMOUNT_PATTERN = re.compile(
    r"(?P<sym>[₹$€£])?\s*"
    r"(?P<code>INR|USD|EUR|GBP|AED|Rs\.?|rupees?|dollars?|euros?|pounds?|dirhams?)?\s*"
    r"(?P<num>\d[\d,]*(?:\.\d{1,2})?)"
    r"\s*(?P<code2>INR|USD|EUR|GBP|AED|Rs\.?|rupees?|dollars?|euros?|pounds?|dirhams?)?",
    re.IGNORECASE,
)

_AMOUNT_CONTEXT_KEYWORDS = ("amount", "total", "sum", "cost", "bill", "price", "paid", "charge")


def extract_amount_currency_from_text(text: str) -> tuple[Optional[float], Optional[str]]:
    """Best-effort parse of a money figure from free text.

    The IDP/extractor step sometimes leaves the structured ``amount`` empty even
    though the figure survives in the email body (e.g. "Total amount: INR 206").
    This backfills it so a real value reaches Stage 4 / Stage 5a instead of a
    misleading 0. Prefers a number adjacent to a currency marker and/or an
    amount keyword; ignores bare numbers like dates.
    """
    if not text:
        return None, None

    best: tuple[int, float, Optional[str]] | None = None  # (priority, amount, currency)
    for m in _AMOUNT_PATTERN.finditer(text):
        try:
            value = float(m.group("num").replace(",", ""))
        except ValueError:
            continue
        token = m.group("sym") or m.group("code") or m.group("code2")
        currency = _CURRENCY_MAP.get(token.strip().lower(), None) if token else None
        context = text[max(0, m.start() - 25): m.start()].lower()
        near_keyword = any(kw in context for kw in _AMOUNT_CONTEXT_KEYWORDS)
        # Skip bare numbers with no currency and no money context (dates, IDs, etc.).
        if currency is None and not near_keyword:
            continue
        priority = (2 if near_keyword else 0) + (1 if currency else 0)
        if best is None or priority > best[0]:
            best = (priority, value, currency)

    if best is None:
        return None, None
    return best[1], best[2]


# ----------------------------------------------------------------------------
# Node 1 - classify (LLM with deterministic fallback)
# ----------------------------------------------------------------------------
async def classify(state: GraphState) -> dict:
    policy = load_policy()
    # The email body and the stated reason are both natural-language signals.
    combined_text = " ".join(filter(None, [state.source_email_body, state.reason]))
    kw_type = classify_expense_type_keywords(combined_text, state.vendor)
    kw_purpose = validate_business_purpose_keywords(combined_text)

    # Backfill amount/currency from the email body when the IDP left them empty,
    # so a real figure reaches Stage 4 / Stage 5a instead of a misleading 0.
    # A structured value from upstream always takes precedence.
    parsed_amount, parsed_currency = extract_amount_currency_from_text(combined_text)
    backfill: dict = {}
    if not state.amount and parsed_amount is not None:
        backfill["amount"] = parsed_amount
    if not state.currency and parsed_currency:
        backfill["currency"] = parsed_currency

    system_prompt = (
        "You are a reimbursement classification agent for an enterprise expense system. "
        "You receive structured expense data extracted from an employee email + IDP receipt scan. "
        "Return strictly valid structured output.\n\n"
        "TASK 1 - CLASSIFY EXPENSE TYPE into exactly one of: "
        "travel | food | medical | internet | equipment | others.\n"
        "  - flight, hotel, train, cab, taxi, airline -> travel\n"
        "  - food, meals, lunch, dinner, snacks, restaurant, cafeteria -> food\n"
        "  - doctor, hospital, medicine, pharmacy, clinic, treatment -> medical\n"
        "  - wifi, broadband, internet, jio, airtel, bsnl -> internet\n"
        "  - laptop, monitor, keyboard, mouse, headset, hardware, equipment -> equipment\n"
        "  - anything else or ambiguous -> others\n\n"
        "TASK 2 - VALIDATE BUSINESS PURPOSE. business_purpose_valid = true if the reason mentions "
        "a work activity, client, project, office, meeting, or official duty. "
        "false if it is empty, personal, entertainment, gaming, or vague.\n\n"
        "TASK 3 - classification_confidence = High | Medium | Low based on how clear the signal is.\n\n"
        "Be fully deterministic (temperature 0)."
    )
    user_prompt = (
        f"Maestro Case ID: {state.case_id}\n"
        f"Employee: {state.employee_email}\n"
        f"Email body: {state.source_email_body or '(empty)'}\n"
        f"IDP vendor: {state.vendor or 'not provided'}\n"
        f"IDP date: {state.date or 'not provided'}\n"
        f"Stated reason: {state.reason or 'not provided'}\n"
        f"IDP amount: {state.amount} {state.currency}\n"
        f"Document attached: {state.document_attached}\n"
        f"OCR confidence: {state.ocr_confidence}\n"
    )

    try:
        # Lazy import + instantiate inside the node (never at module level - auth on import).
        from uipath_langchain.chat.models import UiPathChat

        model_name = policy.get("agent_settings", {}).get("model", "gpt-4o-2024-11-20")
        llm = UiPathChat(model=model_name, temperature=0, max_tokens=1024)
        structured = llm.with_structured_output(_LLMClassification)
        raw = await structured.ainvoke(
            [SystemMessage(system_prompt), HumanMessage(user_prompt)]
        )
        result = (
            raw if isinstance(raw, _LLMClassification) else _LLMClassification.model_validate(raw)
        )
        return {
            **backfill,
            "expense_type": result.expense_type,
            "business_purpose_valid": result.business_purpose_valid,
            "classification_confidence": result.classification_confidence,
        }
    except Exception:
        # No auth / no Agent Units / gateway error -> deterministic keyword classification.
        return {
            **backfill,
            "expense_type": kw_type,
            "business_purpose_valid": kw_purpose,
            "classification_confidence": "High" if kw_type != "others" else "Medium",
        }


# ----------------------------------------------------------------------------
# Node 2 - score_risk (deterministic; terminal node emits full output)
# ----------------------------------------------------------------------------
def score_risk(state: GraphState) -> GraphOutput:
    policy = load_policy()
    rules = policy["risk_rules"]
    expense_type: ExpenseType = state.expense_type or "others"
    amount = state.amount or 0
    body = (state.source_email_body or "").lower()
    bp_valid = bool(state.business_purpose_valid)
    doc = bool(state.document_attached)
    ocr = state.ocr_confidence
    date_missing = state.date is None or str(state.date).strip().lower() in ("", "not provided", "null")

    high_factors: list[str] = []
    medium_factors: list[str] = []

    # ---- HIGH conditions ----
    if expense_type == "medical" and not doc:
        high_factors.append("medical_no_document")
    if expense_type == "travel" and amount > rules["high_amount_threshold"] and not doc:
        high_factors.append("travel_high_amount_no_document")
    if expense_type == "others":
        high_factors.append("expense_type_others")
    if not bp_valid and amount > rules["medium_amount_threshold"]:
        high_factors.append("no_business_purpose_high_amount")
    for phrase in rules["suspicious_phrases"]:
        if phrase in body:
            high_factors.append(f"suspicious_phrase:{phrase.replace(' ', '_')}")
    if state.duplicate_detected:
        high_factors.append("duplicate_claim_suspected")

    # ---- MEDIUM conditions ----
    if not doc and amount > rules["medium_amount_threshold"]:
        medium_factors.append("no_document_high_amount")
    if expense_type == "travel" and rules["travel_medium_band_min"] <= amount <= rules["travel_medium_band_max"]:
        medium_factors.append("travel_mid_amount")
    if ocr is not None and ocr < rules["ocr_confidence_min"]:
        medium_factors.append("low_ocr_confidence")
    if date_missing:
        medium_factors.append("missing_date")

    if high_factors:
        risk_score: RiskScore = "High"
        risk_factors = high_factors + medium_factors
    elif medium_factors:
        risk_score = "Medium"
        risk_factors = medium_factors
    else:
        risk_score = "Low"
        risk_factors = []

    details = (
        f"Classified as {expense_type}, {risk_score} risk"
        + (f" due to: {', '.join(risk_factors)}." if risk_factors else " - no risk factors triggered.")
    )
    audit_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stage": "classification",
        "stage_number": 3,
        "actor": "ReimbursementClassificationAgent",
        "actor_type": "agent",
        "action": "classification_complete",
        "details": details,
    }

    # Terminal node: return ALL output fields (runtime captures only the last delta).
    # Null vendor/date/reason are defaulted so Mir's required Stage 4 inputs are always populated.
    return GraphOutput(
        case_id=state.case_id,
        expense_type=expense_type,
        amount=amount,
        currency=state.currency or "INR",
        vendor=state.vendor or "not provided",
        date=str(state.date) if state.date else "not provided",
        reason=state.reason or state.source_email_body or "not provided",
        document_attached=doc,
        risk_score=risk_score,
        duplicate_detected=state.duplicate_detected,
        business_purpose_valid=bp_valid,
        employee_name=derive_employee_name(state.employee_name, state.employee_email),
        employee_email=state.employee_email or "not provided",
        risk_factors=risk_factors,
        classification_confidence=state.classification_confidence,
        audit_entry=audit_entry,
    )


# ----------------------------------------------------------------------------
# Graph
# ----------------------------------------------------------------------------
builder = StateGraph(GraphState, input_schema=GraphInput, output_schema=GraphOutput)
builder.add_node("classify", classify)
builder.add_node("score_risk", score_risk)
builder.add_edge(START, "classify")
builder.add_edge("classify", "score_risk")
builder.add_edge("score_risk", END)

graph = builder.compile()
