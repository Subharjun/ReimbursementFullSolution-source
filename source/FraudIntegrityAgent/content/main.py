"""
AgentHack Track 1 - Reimbursement Fraud / Integrity Agent.

Owner: Subharjun.

A UiPath **coded LangGraph agent** that screens a reimbursement claim for fraud
and integrity risk BEFORE it reaches payout. It is the pipeline's answer to the
"invoice processing is commoditised" critique: a plain OCR/AP tool reads a
receipt, but this agent *reasons* about whether the claim should be trusted --
catching duplicate resubmissions, split claims that dodge the approval limit,
threshold-hugging, and abnormal claim velocity that a rules-only reader misses.

Design discipline -- **the code decides, the LLM only explains**:
  - `detect`  : PURE deterministic detectors (`detectors.py`, no LLM, no I/O) turn
                the claim (+ the claimant's recent history) into a fraud score, a
                risk band, and a list of concrete flags with transparent thresholds.
                Every verdict is reproducible and auditable -- never a black box.
  - `explain` : the LLM (UiPath LLM Gateway, gpt-4o) writes a short, professional
                investigator-style narrative *of the flags the detector produced*.
                It NEVER invents a flag, number, or verdict -- if the LLM/Agent
                Units are unavailable it falls back to a deterministic summary, so
                the agent still runs locally with no auth.

Graph:  START -> detect (deterministic) -> explain (LLM prose) -> END

Output is a superset drop-in for the Maestro Case: the structured verdict fields
plus `duplicate_detected` / `split_claim_detected` -- the exact signals the
NotificationAgent's ROI model credits as "duplicate double-payment prevented",
so this agent makes that saving *earned* rather than assumed. No Integration
Service connection is required (pure analysis), so there are no bindings.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from detectors import compute_integrity, summary_line

# Model used to write the explanation (prose only). Routed via UiPath LLM Gateway.
EXPLAIN_MODEL = "gpt-4o-2024-11-20"


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class GraphInput(BaseModel):
    """The claim under review plus the claimant's recent prior claims.

    `claim_history` keeps the agent stateless and serverless-safe: duplicate /
    split / velocity detection needs prior claims, and passing them in (rather
    than reaching into a DB) makes every run reproducible and easy to demo. A
    real deployment would populate it from a Data Fabric entity or a queue."""

    case_id: str = Field(default="", description="Maestro case ID of the claim under review")
    employee_email: str = Field(default="", description="Claimant email (used for velocity checks)")
    employee_name: str = Field(default="", description="Claimant name (context only)")
    vendor: str = Field(default="", description="Vendor / merchant on the claim")
    amount: float = Field(default=0.0, description="Claimed amount")
    currency: str = Field(default="USD", description="ISO currency code")
    date: str = Field(default="", description="Expense date (YYYY-MM-DD or MM/DD/YYYY)")
    expense_type: str = Field(default="", description="Classified expense type (context only)")
    reason: str = Field(default="", description="Claim reason / purpose (context only)")

    claim_history: list[dict] = Field(
        default_factory=list,
        description="Recent prior claims [{vendor, amount, currency, date, employee_email, case_id}] "
        "for duplicate / split / velocity detection. Empty is valid (no history available).",
    )


class GraphOutput(BaseModel):
    fraud_score: int = 0
    integrity_risk: str = "Low"          # Low | Medium | High
    recommendation: str = "proceed"      # proceed | review | reject
    duplicate_detected: bool = False
    split_claim_detected: bool = False
    flags: list[dict] = Field(default_factory=list)
    explanation: str = ""                # human-readable narrative (LLM or fallback)
    summary: str = ""                    # one-line summary
    assumptions: dict = Field(default_factory=dict)


class GraphState(BaseModel):
    # mirror of the input
    case_id: str = ""
    employee_email: str = ""
    employee_name: str = ""
    vendor: str = ""
    amount: float = 0.0
    currency: str = "USD"
    date: str = ""
    expense_type: str = ""
    reason: str = ""
    claim_history: list[dict] = Field(default_factory=list)
    # produced by detect
    result: dict = Field(default_factory=dict)


def _claim_dict(state: GraphState) -> dict:
    return {
        "case_id": state.case_id,
        "employee_email": state.employee_email,
        "vendor": state.vendor,
        "amount": state.amount,
        "currency": state.currency,
        "date": state.date,
    }


def _fallback_explanation(result: dict, name: str) -> str:
    """Deterministic narrative used when the LLM is unavailable."""
    flags = result.get("flags", [])
    who = name or "this claimant"
    if not flags:
        return (
            f"No integrity concerns were found for {who}'s claim. The amount, vendor, "
            "date and recent history all look consistent, so it is safe to proceed."
        )
    lead = {
        "High": "This claim should be rejected or escalated.",
        "Medium": "This claim warrants a manual review before payout.",
        "Low": "This claim looks acceptable, with only minor notes.",
    }.get(result.get("integrity_risk"), "Review the flags below.")
    reasons = " ".join(f["detail"] for f in flags[:3])
    return f"{lead} {reasons}"


# --------------------------------------------------------------------------- #
# Node 1 - detect (PURE deterministic; the verdict is fixed here)
# --------------------------------------------------------------------------- #
async def detect(state: GraphState) -> dict:
    result = compute_integrity(_claim_dict(state), state.claim_history)
    return {"result": result}


# --------------------------------------------------------------------------- #
# Node 2 - explain (LLM writes PROSE ONLY over the detector's flags; fallback)
# --------------------------------------------------------------------------- #
async def explain(state: GraphState) -> GraphOutput:
    result = state.result or compute_integrity(_claim_dict(state), state.claim_history)
    name = state.employee_name or (state.employee_email.split("@")[0] if state.employee_email else "")

    explanation = _fallback_explanation(result, name)

    # Only bother the LLM when there is something to narrate.
    flags = result.get("flags", [])
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from uipath_langchain.chat.models import UiPathChat

        flag_lines = "\n".join(f"- [{f['severity'].upper()}] {f['title']}: {f['detail']}" for f in flags) or "- (none)"
        system_prompt = (
            "You are a meticulous corporate expense-integrity analyst. You are given the "
            "RESULT of a deterministic fraud screen (a risk band and a list of flags). "
            "Write a short, professional explanation (2-4 sentences) a finance reviewer can "
            "act on.\n"
            "STRICT RULES:\n"
            "- Explain ONLY the flags provided. Do NOT invent new findings, numbers, or claims.\n"
            "- Do NOT contradict the given risk band or recommendation.\n"
            "- Be specific and neutral; describe risk, do not accuse a person.\n"
            "- If there are no flags, say the claim looks clean and can proceed.\n"
            "- No placeholders, no markdown headers, no bullet list -- plain prose.\n"
            "- Output only the explanation text."
        )
        user_prompt = (
            f"Risk band: {result.get('integrity_risk')}\n"
            f"Recommendation: {result.get('recommendation')}\n"
            f"Fraud score: {result.get('fraud_score')}/100\n"
            f"Claim: vendor={state.vendor!r}, amount={state.amount} {state.currency}, "
            f"date={state.date!r}, type={state.expense_type!r}\n"
            f"Flags:\n{flag_lines}\n\n"
            "Write the explanation now."
        )
        llm = UiPathChat(model=EXPLAIN_MODEL, temperature=0.3, max_tokens=200)
        resp = await llm.ainvoke([SystemMessage(system_prompt), HumanMessage(user_prompt)])
        text = (getattr(resp, "content", "") or "").strip()
        if text:
            explanation = text if len(text) <= 700 else text[:697].rstrip() + "..."
    except Exception:
        pass  # keep the deterministic fallback

    return GraphOutput(
        fraud_score=int(result.get("fraud_score", 0)),
        integrity_risk=result.get("integrity_risk", "Low"),
        recommendation=result.get("recommendation", "proceed"),
        duplicate_detected=bool(result.get("duplicate_detected", False)),
        split_claim_detected=bool(result.get("split_claim_detected", False)),
        flags=flags,
        explanation=explanation,
        summary=summary_line(result),
        assumptions=result.get("assumptions", {}),
    )


# --------------------------------------------------------------------------- #
# Graph
# --------------------------------------------------------------------------- #
builder = StateGraph(GraphState, input_schema=GraphInput, output_schema=GraphOutput)
builder.add_node("detect", detect)
builder.add_node("explain", explain)
builder.add_edge(START, "detect")
builder.add_edge("detect", "explain")
builder.add_edge("explain", END)

graph = builder.compile()
