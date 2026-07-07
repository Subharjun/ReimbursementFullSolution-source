"""
AgentHack Track 1 - Reimbursement Fraud / Integrity Agent.

Owner: Subharjun.

A UiPath **coded LangGraph agent** that screens a reimbursement claim for fraud
and integrity risk BEFORE it reaches payout. It is the pipeline's answer to the
"invoice processing is commoditised" critique: a plain OCR/AP tool reads a
receipt, but this agent *reasons* about whether the claim should be trusted --
catching duplicate resubmissions, split claims that dodge the approval limit,
threshold-hugging, and abnormal claim velocity that a rules-only reader misses.

Design discipline -- **deterministic floor, agentic judgment on top**:
  - `detect`  : PURE deterministic detectors (`detectors.py`, no LLM, no I/O) turn
                the claim (+ the claimant's recent history) into a fraud score, a
                risk band, and a list of concrete flags with transparent thresholds.
                Every signal is reproducible and auditable -- never a black box.
                This is the FLOOR: the agent may never rate a claim *safer* than the
                detectors did, and may never invent a duplicate/split that the
                detectors did not actually find.
  - `judge`   : the LLM (UiPath LLM Gateway, gpt-4o) *synthesises* the deterministic
                signals into a final judgment -- it may ESCALATE the risk band /
                recommendation when several weak signals compound in a way fixed
                thresholds miss (e.g. a round-amount + weekend + threshold-hug that
                individually score low but together smell wrong), and it writes the
                investigator-style rationale. A monotonic server-side guardrail then
                clamps the LLM's call so it can only ever tighten, never loosen, the
                deterministic verdict, and the duplicate/split flags stay exactly what
                the detectors returned. If the LLM/Agent Units are unavailable it
                falls back to the pure deterministic verdict, so the agent still runs
                locally with no auth.

Graph:  START -> detect (deterministic floor) -> judge (LLM judgment + guardrail) -> END

Output is a superset drop-in for the Maestro Case: the structured verdict fields
plus `duplicate_detected` / `split_claim_detected` -- the exact signals the
NotificationAgent's ROI model credits as "duplicate double-payment prevented",
so this agent makes that saving *earned* rather than assumed. No Integration
Service connection is required (pure analysis), so there are no bindings.
"""

from __future__ import annotations

import json
import re

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from detectors import compute_integrity, summary_line

# Model used to synthesise the judgment + rationale. Routed via UiPath LLM Gateway.
JUDGE_MODEL = "gpt-4o-2024-11-20"

# Monotonic severity ranks. The guardrail uses these to enforce that the LLM's
# judgment can only ever ESCALATE (tighten) the deterministic verdict, never
# soften it -- so an agent hallucination can never wave through a flagged claim.
_RISK_RANK = {"Low": 0, "Medium": 1, "High": 2}
_RANK_RISK = {0: "Low", 1: "Medium", 2: "High"}
_REC_RANK = {"proceed": 0, "review": 1, "reject": 2}
_RANK_REC = {0: "proceed", 1: "review", 2: "reject"}


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
    judged_by: str = "deterministic"     # 'llm' if the agent synthesised the verdict, else 'deterministic'
    escalated: bool = False              # True if the agent tightened the band above the deterministic floor


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


def _extract_json(text: str) -> dict | None:
    """Tolerantly pull the first JSON object out of an LLM reply."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# Node 2 - judge (LLM SYNTHESISES the verdict; monotonic guardrail clamps it)
# --------------------------------------------------------------------------- #
async def judge(state: GraphState) -> GraphOutput:
    result = state.result or compute_integrity(_claim_dict(state), state.claim_history)
    name = state.employee_name or (state.employee_email.split("@")[0] if state.employee_email else "")
    flags = list(result.get("flags", []))

    # The deterministic FLOOR -- the guardrail never lets the final verdict fall
    # below this, and the duplicate/split flags below are read-only for the LLM.
    det_risk = result.get("integrity_risk", "Low")
    det_rec = result.get("recommendation", "proceed")
    det_score = int(result.get("fraud_score", 0))
    assumptions = result.get("assumptions", {}) or {}
    high_at = int(assumptions.get("high_at", 60))
    medium_at = int(assumptions.get("medium_at", 25))

    # Defaults (used verbatim if the LLM/Agent Units are unavailable).
    final_risk, final_rec, final_score = det_risk, det_rec, det_score
    explanation = _fallback_explanation(result, name)
    judged_by = "deterministic"
    escalated = False

    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from uipath_langchain.chat.models import UiPathChat

        flag_lines = "\n".join(
            f"- [{f['severity'].upper()}] {f['code']} — {f['title']}: {f['detail']}" for f in flags
        ) or "- (no deterministic signals fired)"
        system_prompt = (
            "You are a senior corporate expense-integrity analyst reviewing the output of a "
            "deterministic fraud screen. The screen already computed a risk band, a "
            "recommendation, and a list of concrete flags with transparent thresholds.\n"
            "Your job is to SYNTHESISE a final judgment — you are allowed to be smarter than "
            "fixed thresholds: when several individually-weak signals COMPOUND into a pattern "
            "that genuinely smells wrong, you may ESCALATE the risk band and/or recommendation.\n"
            "HARD RULES (a downstream guardrail also enforces these):\n"
            "- You may only ESCALATE (make stricter), NEVER soften. You cannot rate the claim "
            "lower-risk than the screen did, and cannot downgrade its recommendation.\n"
            "- Allowed integrity_risk values: Low, Medium, High. Allowed recommendation values: "
            "proceed, review, reject. Order of strictness: Low<Medium<High, proceed<review<reject.\n"
            "- Do NOT invent a duplicate or split-claim that the flags do not show. Base every "
            "claim of fact strictly on the flags provided.\n"
            "- If the flags are clean, keep it clean (proceed / Low) — do not manufacture risk.\n"
            "Respond with ONLY a JSON object, no prose around it:\n"
            '{"integrity_risk": "...", "recommendation": "...", "rationale": "2-4 sentence '
            'investigator narrative a finance reviewer can act on"}'
        )
        user_prompt = (
            f"Deterministic screen result:\n"
            f"- risk band: {det_risk}\n"
            f"- recommendation: {det_rec}\n"
            f"- fraud score: {det_score}/100 (Medium band starts at {medium_at}, High at {high_at})\n"
            f"- duplicate_detected: {result.get('duplicate_detected', False)}\n"
            f"- split_claim_detected: {result.get('split_claim_detected', False)}\n"
            f"Claim: vendor={state.vendor!r}, amount={state.amount} {state.currency}, "
            f"date={state.date!r}, type={state.expense_type!r}\n"
            f"Flags:\n{flag_lines}\n\n"
            "Return your judgment JSON now."
        )
        llm = UiPathChat(model=JUDGE_MODEL, temperature=0.2, max_tokens=320)
        resp = await llm.ainvoke([SystemMessage(system_prompt), HumanMessage(user_prompt)])
        obj = _extract_json(getattr(resp, "content", "") or "")
        if obj:
            judged_by = "llm"
            llm_risk_rank = _RISK_RANK.get(str(obj.get("integrity_risk", "")).strip().title(), 0)
            llm_rec_rank = _REC_RANK.get(str(obj.get("recommendation", "")).strip().lower(), 0)
            # MONOTONIC GUARDRAIL: max() with the deterministic floor -> can only tighten.
            final_risk = _RANK_RISK[max(_RISK_RANK[det_risk], llm_risk_rank)]
            final_rec = _RANK_REC[max(_REC_RANK[det_rec], llm_rec_rank)]
            escalated = (final_risk != det_risk) or (final_rec != det_rec)
            # Keep the numeric score consistent with an escalated band so the UI gauge
            # doesn't show e.g. "High risk, score 20/100".
            band_floor = {"High": high_at, "Medium": medium_at, "Low": 0}[final_risk]
            final_score = min(100, max(det_score, band_floor))
            rationale = str(obj.get("rationale", "")).strip()
            if rationale:
                explanation = rationale if len(rationale) <= 700 else rationale[:697].rstrip() + "..."
    except Exception:
        pass  # keep the deterministic floor verbatim

    # If the agent escalated beyond the fixed thresholds, record it as an explicit,
    # clearly-agent-derived flag so the escalation is auditable (not a silent nudge).
    if escalated:
        flags = flags + [{
            "code": "AGENT_ESCALATION",
            "severity": "high" if final_risk == "High" else "medium",
            "title": "Analyst escalation",
            "detail": (
                f"The integrity agent escalated this claim from {det_risk}/{det_rec} to "
                f"{final_risk}/{final_rec} after weighing how the signals compound — beyond "
                f"the fixed deterministic thresholds."
            ),
        }]

    # Reflect the (possibly escalated) verdict back into the result dict so the
    # one-line summary and the returned flags/score are internally consistent.
    result = dict(result)
    result["integrity_risk"] = final_risk
    result["recommendation"] = final_rec
    result["fraud_score"] = final_score
    result["flags"] = flags

    return GraphOutput(
        fraud_score=int(final_score),
        integrity_risk=final_risk,
        recommendation=final_rec,
        duplicate_detected=bool(result.get("duplicate_detected", False)),
        split_claim_detected=bool(result.get("split_claim_detected", False)),
        flags=flags,
        explanation=explanation,
        summary=summary_line(result),
        assumptions=assumptions,
        judged_by=judged_by,
        escalated=escalated,
    )


# --------------------------------------------------------------------------- #
# Graph
# --------------------------------------------------------------------------- #
builder = StateGraph(GraphState, input_schema=GraphInput, output_schema=GraphOutput)
builder.add_node("detect", detect)
builder.add_node("judge", judge)
builder.add_edge(START, "detect")
builder.add_edge("detect", "judge")
builder.add_edge("judge", END)

graph = builder.compile()
