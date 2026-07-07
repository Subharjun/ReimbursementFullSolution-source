"""
AgentHack Track 1 - Consensus Arbitration Agent.

Owner: Subharjun.

A UiPath **coded LangGraph agent** that makes the FINAL reimbursement decision by
synthesising the three upstream reasoning stages -- Classifier, Fraud Sentinel,
Policy Arbiter -- into one defensible verdict, and writes the rationale that
references all three. It replaces the pipeline's old deterministic precedence
code (`debate._arbitrate`) with a genuinely agentic final judge, WITHOUT giving
an LLM the keys to the vault:

Design discipline -- **hard compliance floor, agentic judgment on top**:
  - `baseline` : PURE deterministic precedence (the exact rules the pipeline has
                 always used: spend-limit veto > confirmed-duplicate reject >
                 fraud reject > fraud/split review > low-confidence/manager
                 escalation > auto-approve > proceed). This is the FLOOR.
  - `decide`   : the LLM (UiPath LLM Gateway, gpt-4o) weighs all three stages and
                 issues the final call + a synthesised rationale. A monotonic
                 server-side guardrail then clamps it: the LLM may only make the
                 verdict STRICTER than the deterministic floor, never softer. So
                 non-negotiable compliance blocks (over spend limit, confirmed
                 duplicate, hard policy reject) can NEVER be soft-overridden by an
                 LLM -- it is mathematically impossible for the agent to approve
                 something the rules reject. If the LLM/Agent Units are
                 unavailable, the deterministic floor IS the verdict, so the agent
                 still runs locally with no auth.

Graph:  START -> baseline (deterministic floor) -> decide (LLM + guardrail) -> END

Output matches the pipeline's arbitration contract exactly -- recommendation
(REJECT | HITL_REVIEW | AUTO_APPROVE | PROCEED_REVIEWED), path (reject |
hitl_review | auto_approve | proceed), rationale -- so it is a drop-in for
`debate._arbitrate` with nothing downstream to change.
"""

from __future__ import annotations

import json
import re

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

# Model used for the final synthesis. Routed via UiPath LLM Gateway.
DECIDE_MODEL = "gpt-4o-2024-11-20"

# The four terminal verdicts and their canonical path slugs, ordered by
# strictness. The guardrail uses this order to enforce that the LLM may only
# ever ESCALATE the deterministic floor, never loosen it.
_REC_TO_PATH = {
    "AUTO_APPROVE": "auto_approve",
    "PROCEED_REVIEWED": "proceed",
    "HITL_REVIEW": "hitl_review",
    "REJECT": "reject",
}
_REC_RANK = {"AUTO_APPROVE": 0, "PROCEED_REVIEWED": 1, "HITL_REVIEW": 2, "REJECT": 3}
_RANK_REC = {0: "AUTO_APPROVE", 1: "PROCEED_REVIEWED", 2: "HITL_REVIEW", 3: "REJECT"}


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class GraphInput(BaseModel):
    """The three upstream stage findings + a little claim context. Mirrors the
    dicts the Consensus Engine already computes for Classify / Fraud / Policy."""

    case_id: str = Field(default="", description="Maestro case ID of the claim under arbitration")
    vendor: str = Field(default="", description="Vendor / merchant on the claim (context)")
    amount: float = Field(default=0.0, description="Claimed amount (context)")
    currency: str = Field(default="USD", description="ISO currency code (context)")
    expense_type: str = Field(default="", description="Classified expense type (context)")

    # --- Classifier stage ---
    classification_confidence: str = Field(default="Medium", description="Classifier confidence: Low | Medium | High")
    risk_score: str = Field(default="Low", description="Classifier risk band: Low | Medium | High")

    # --- Fraud Sentinel stage ---
    fraud_score: int = Field(default=0, description="Fraud score 0-100")
    integrity_risk: str = Field(default="Low", description="Fraud risk band: Low | Medium | High")
    fraud_recommendation: str = Field(default="proceed", description="Fraud call: proceed | review | reject")
    duplicate_detected: bool = Field(default=False, description="A confirmed duplicate of a prior claim")
    split_claim_detected: bool = Field(default=False, description="A split-claim pattern dodging the approval limit")

    # --- Policy Arbiter stage ---
    within_spend_limit: bool = Field(default=True, description="Amount is within the category spend limit")
    policy_routing: str = Field(default="auto_approve", description="Policy routing: auto_approve | manager_review | reject")
    policy_violations: list[str] = Field(default_factory=list, description="Named policy violations, if any")
    spend_limit: float = Field(default=0.0, description="Category spend limit (context)")
    auto_approve_threshold: float = Field(default=0.0, description="Category auto-approve threshold (context)")


class GraphOutput(BaseModel):
    recommendation: str = "PROCEED_REVIEWED"  # REJECT | HITL_REVIEW | AUTO_APPROVE | PROCEED_REVIEWED
    path: str = "proceed"                      # reject | hitl_review | auto_approve | proceed
    rationale: str = ""
    decided_by: str = "deterministic"          # 'llm' if the agent synthesised it, else 'deterministic'
    escalated: bool = False                    # True if the agent tightened above the deterministic floor
    audit: dict = Field(default_factory=dict)  # the deterministic floor + compliance blocks, for a transparent trail


class GraphState(BaseModel):
    case_id: str = ""
    vendor: str = ""
    amount: float = 0.0
    currency: str = "USD"
    expense_type: str = ""
    classification_confidence: str = "Medium"
    risk_score: str = "Low"
    fraud_score: int = 0
    integrity_risk: str = "Low"
    fraud_recommendation: str = "proceed"
    duplicate_detected: bool = False
    split_claim_detected: bool = False
    within_spend_limit: bool = True
    policy_routing: str = "auto_approve"
    policy_violations: list[str] = Field(default_factory=list)
    spend_limit: float = 0.0
    auto_approve_threshold: float = 0.0
    # produced by baseline
    floor_rec: str = ""
    floor_path: str = ""
    floor_rationale: str = ""


# --------------------------------------------------------------------------- #
# Deterministic precedence -- the exact rules the pipeline has always used.
# This is BOTH the compliance floor (the LLM may never go below it) AND the
# fallback verdict when the LLM is unavailable. Ported verbatim from
# consensus/debate.py `_arbitrate` so the two can never silently drift.
# --------------------------------------------------------------------------- #
def _deterministic(state: GraphState) -> tuple[str, str, str]:
    if (not state.within_spend_limit) or state.duplicate_detected or state.policy_routing == "reject":
        why = []
        if not state.within_spend_limit:
            why.append("over the category spend limit")
        if state.duplicate_detected:
            why.append("a confirmed duplicate (double-payment risk)")
        if state.policy_routing == "reject" and not why:
            why.append("a hard policy violation")
        return "REJECT", "reject", "Blocked by " + " and ".join(why) + "."
    if state.fraud_recommendation == "reject":
        return "REJECT", "reject", f"Fraud Sentinel scored this {state.fraud_score}/100 (High) — too risky to pay."
    if state.split_claim_detected or state.fraud_recommendation == "review":
        return "HITL_REVIEW", "hitl_review", "Fraud signals warrant a human reviewer before any payout."
    if state.classification_confidence == "Low" or state.policy_routing == "manager_review":
        reason = "low classification confidence" if state.classification_confidence == "Low" else "policy requires manager review"
        return "HITL_REVIEW", "hitl_review", f"Escalating to a human because of {reason}."
    if state.policy_routing == "auto_approve":
        return "AUTO_APPROVE", "auto_approve", "Clean on all three fronts and under the auto-approve threshold — straight through to payout."
    return "PROCEED_REVIEWED", "proceed", "No violations or fraud signals; proceeds to payout after a light review."


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
# Node 1 - baseline (deterministic compliance floor)
# --------------------------------------------------------------------------- #
async def baseline(state: GraphState) -> dict:
    rec, path, rationale = _deterministic(state)
    return {"floor_rec": rec, "floor_path": path, "floor_rationale": rationale}


# --------------------------------------------------------------------------- #
# Node 2 - decide (LLM synthesises the final call; monotonic guardrail clamps it)
# --------------------------------------------------------------------------- #
async def decide(state: GraphState) -> GraphOutput:
    floor_rec = state.floor_rec or _deterministic(state)[0]
    floor_rank = _REC_RANK[floor_rec]

    final_rec = floor_rec
    rationale = state.floor_rationale or _deterministic(state)[2]
    decided_by = "deterministic"
    escalated = False

    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from uipath_langchain.chat.models import UiPathChat

        viol = ", ".join(state.policy_violations) if state.policy_violations else "none"
        system_prompt = (
            "You are the final arbiter of a corporate reimbursement pipeline. Three specialist "
            "agents have already reported: a Classifier (expense type + risk + confidence), a "
            "Fraud Sentinel (fraud score + duplicate/split detection), and a Policy Arbiter "
            "(spend-limit + routing). Synthesise their findings into ONE final verdict and a "
            "short rationale that explicitly references the relevant evidence from each.\n"
            "Allowed verdicts (in increasing strictness):\n"
            "  AUTO_APPROVE  — clean, low-risk, under the auto-approve threshold; pay straight through.\n"
            "  PROCEED_REVIEWED — no blocking issues but pay only after a light review.\n"
            "  HITL_REVIEW   — send to a human reviewer before any payout.\n"
            "  REJECT        — do not pay.\n"
            "HARD RULES (a downstream guardrail also enforces these — you cannot bypass them):\n"
            "- You may only make the verdict STRICTER than the compliance baseline you are given, "
            "NEVER softer. If the baseline is REJECT, you must REJECT. If it is HITL_REVIEW you may "
            "only choose HITL_REVIEW or REJECT.\n"
            "- Over-spend-limit, a confirmed duplicate, and a hard policy reject are non-negotiable "
            "rejections — never approve them.\n"
            "- You add value by ESCALATING borderline claims the fixed rules would wave through when "
            "the combined picture is genuinely concerning; otherwise agree with the baseline.\n"
            "Respond with ONLY a JSON object, no prose around it:\n"
            '{"recommendation": "ONE OF THE FOUR VERDICTS", "rationale": "2-4 sentences citing the '
            'classifier, fraud and policy evidence"}'
        )
        user_prompt = (
            f"Compliance baseline (the floor — you may not go softer than this): {floor_rec}\n\n"
            f"Claim: {state.amount} {state.currency} to {state.vendor!r}, type={state.expense_type!r}\n"
            f"Classifier: risk={state.risk_score}, confidence={state.classification_confidence}\n"
            f"Fraud Sentinel: score={state.fraud_score}/100 ({state.integrity_risk}), "
            f"call={state.fraud_recommendation}, duplicate={state.duplicate_detected}, "
            f"split_claim={state.split_claim_detected}\n"
            f"Policy Arbiter: routing={state.policy_routing}, within_spend_limit={state.within_spend_limit}, "
            f"spend_limit={state.spend_limit}, auto_approve_threshold={state.auto_approve_threshold}, "
            f"violations={viol}\n\n"
            "Return your verdict JSON now."
        )
        llm = UiPathChat(model=DECIDE_MODEL, temperature=0.2, max_tokens=320)
        resp = await llm.ainvoke([SystemMessage(system_prompt), HumanMessage(user_prompt)])
        obj = _extract_json(getattr(resp, "content", "") or "")
        if obj:
            decided_by = "llm"
            llm_rec = str(obj.get("recommendation", "")).strip().upper()
            llm_rank = _REC_RANK.get(llm_rec, floor_rank)
            # MONOTONIC GUARDRAIL: max() with the deterministic floor -> can only tighten.
            final_rank = max(floor_rank, llm_rank)
            final_rec = _RANK_REC[final_rank]
            escalated = final_rank > floor_rank
            llm_rationale = str(obj.get("rationale", "")).strip()
            if llm_rationale:
                rationale = llm_rationale if len(llm_rationale) <= 700 else llm_rationale[:697].rstrip() + "..."
            # If the guardrail overrode the LLM back UP to the floor (it tried to soften),
            # keep the deterministic rationale so the stated reason matches the verdict.
            if llm_rank < floor_rank:
                rationale = state.floor_rationale or _deterministic(state)[2]
    except Exception:
        pass  # keep the deterministic floor verbatim

    return GraphOutput(
        recommendation=final_rec,
        path=_REC_TO_PATH[final_rec],
        rationale=rationale,
        decided_by=decided_by,
        escalated=escalated,
        audit={
            "deterministic_floor": floor_rec,
            "floor_path": _REC_TO_PATH[floor_rec],
            "compliance_blocks": {
                "over_spend_limit": not state.within_spend_limit,
                "duplicate_detected": state.duplicate_detected,
                "policy_reject": state.policy_routing == "reject",
                "fraud_reject": state.fraud_recommendation == "reject",
            },
        },
    )


# --------------------------------------------------------------------------- #
# Graph
# --------------------------------------------------------------------------- #
builder = StateGraph(GraphState, input_schema=GraphInput, output_schema=GraphOutput)
builder.add_node("baseline", baseline)
builder.add_node("decide", decide)
builder.add_edge(START, "baseline")
builder.add_edge("baseline", "decide")
builder.add_edge("decide", END)

graph = builder.compile()
