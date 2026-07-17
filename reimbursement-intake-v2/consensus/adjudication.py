"""
Adjudication -- the structured decision model behind the Reviewer Cockpit.

Owner: Subharjun. AgentHack Track 1.

WHY THIS EXISTS
    The stock Action Center approval app offers a reviewer two buttons and a
    free-text box. That is enough to move a task off a queue and nowhere near
    enough to defend a payout six months later. "Approved — looks fine" is not
    a reason; it is the absence of one.

    This module makes a human review a STRUCTURED, VALIDATED, ATTRIBUTABLE act:

      * four decisions, not two -- a reviewer who thinks 8,000 of a 12,000
        claim is legitimate should not have to choose between paying all of it
        and paying none of it. Partial approval is the honest answer and the
        stock app cannot express it.
      * a reason code drawn from a closed vocabulary, checked against the
        decision it is attached to (you cannot reject for `within_policy`).
      * evidence obligations that scale with consequence -- a rejection must
        cite the policy clause it rests on; a conditional approval must say
        what the condition is; a partial must say what amount and survive the
        arithmetic.
      * THE OVERRIDE GATE (the point of the whole module): when a reviewer
        contradicts the committee's recommendation, they are not blocked --
        they are made to author a real justification and explicitly acknowledge
        the override. Four agents reasoned about this claim in the open; a
        human may absolutely overrule them, but not silently and not by
        accident. That justification lands in the hash-chained Decision Ledger
        next to the verdict it overturned.

    The intent is not friction for its own sake. Every obligation here is one a
    human auditor would demand anyway -- the system just refuses to let it be
    skipped and then reconstructed from memory later.

DESIGN
    Pure functions over plain dicts, no I/O, no framework types. `validate()`
    returns field-level errors (never raises) so the API layer can answer 422
    with something a form can actually render, and so the rules are unit-
    testable without a tenant, a token, or a running Orchestrator.
"""

from __future__ import annotations

from typing import Any

# ── Vocabulary ────────────────────────────────────────────────────────────────

# The four decisions a reviewer can reach. `partial_approve` and
# `approve_with_conditions` are the two the stock app cannot express, and they
# are the two that most often reflect what a reviewer actually believes.
DECISIONS = ("approve", "partial_approve", "approve_with_conditions", "reject")

# Which Action Center outcome each decision maps to. The Case only understands
# Approve/Reject -- the nuance lives in the structured payload we send with it,
# so the cockpit stays richer than the Case without needing the Case to change.
_OUTCOME = {
    "approve": "approve",
    "partial_approve": "approve",
    "approve_with_conditions": "approve",
    "reject": "reject",
}

# Reason codes, partitioned by the decision they may justify. The partition IS
# the validation: a closed vocabulary that is merely *listed* gets used as a
# dropdown of synonyms; one that is *checked against the decision* catches the
# reviewer who picked a reject-reason and then clicked Approve.
REASON_CODES: dict[str, dict[str, str]] = {
    "approve": {
        "within_policy": "Within policy — no exceptions required",
        "documentation_sufficient": "Documentation complete and legible",
        "business_purpose_verified": "Business purpose verified with the claimant",
        "ai_flag_unfounded": "Agent flag reviewed and found unfounded",
        "prior_approval_on_file": "Pre-approved spend, approval on file",
    },
    "partial_approve": {
        "partial_over_limit": "Reimbursed to the policy limit; excess declined",
        "partial_unsupported_portion": "Portion of the claim is unsupported by the receipt",
        "partial_non_reimbursable_items": "Receipt includes non-reimbursable line items",
        "partial_personal_share": "Personal share separated from the business share",
    },
    "approve_with_conditions": {
        "conditional_pending_receipt": "Approved pending a legible receipt",
        "conditional_manager_signoff": "Approved subject to manager sign-off",
        "conditional_one_time_exception": "One-time exception granted; not precedent",
        "conditional_cost_centre_correction": "Approved subject to cost-centre correction",
    },
    "reject": {
        "over_spend_limit": "Exceeds the policy spend limit",
        "duplicate_claim": "Duplicate of a previously submitted claim",
        "insufficient_documentation": "Receipt missing, illegible, or non-itemised",
        "not_business_related": "Not a business expense",
        "policy_violation": "Violates an expense policy clause",
        "suspected_fraud": "Integrity concern — escalated",
        "receipt_mismatch": "Receipt does not corroborate the claimed amount or vendor",
        "outside_claim_window": "Submitted outside the claim window",
    },
}

# Minimum characters for the free-text obligations. Long enough to force a
# sentence, short enough not to punish a reviewer who is genuinely brief.
_MIN_CONDITIONS = 10
_MIN_OVERRIDE_JUSTIFICATION = 25
_MIN_REJECT_CITATION = 3


def reason_catalog() -> dict[str, Any]:
    """The vocabulary, shaped for the form. Served to the cockpit so the
    dropdown and the validator can never drift apart -- there is exactly one
    definition of a legal reason code and both ends read it."""
    return {
        "decisions": [
            {"value": "approve", "label": "Approve in full",
             "hint": "Pay the claim as submitted."},
            {"value": "partial_approve", "label": "Approve in part",
             "hint": "Pay a reduced amount; the balance is declined."},
            {"value": "approve_with_conditions", "label": "Approve with conditions",
             "hint": "Pay, subject to a condition the claimant must meet."},
            {"value": "reject", "label": "Reject",
             "hint": "Decline the claim. Requires a policy citation."},
        ],
        "reason_codes": REASON_CODES,
        "limits": {
            "min_conditions": _MIN_CONDITIONS,
            "min_override_justification": _MIN_OVERRIDE_JUSTIFICATION,
            "min_reject_citation": _MIN_REJECT_CITATION,
        },
    }


# ── Override detection ────────────────────────────────────────────────────────

def outcome_of(decision: str) -> str:
    """The Action Center outcome ('approve'/'reject') a decision resolves to."""
    return _OUTCOME.get(decision, "approve")


def is_override(decision: str, ai_lean: str) -> bool:
    """True when the reviewer's call contradicts what the committee leaned to.

    `uncertain` is never an override: the committee declining to take a side
    cannot be contradicted, and treating it as one would demand justification
    for the exact cases where the AI offered no opinion to overrule -- which
    would train reviewers to write filler. A partial approval against an
    approve-lean is likewise not an override; the reviewer agreed on the
    direction and refined the amount, which is the system working.
    """
    if ai_lean not in ("approve", "reject"):
        return False
    return outcome_of(decision) != ai_lean


# ── Validation ────────────────────────────────────────────────────────────────

def _article(decision: str) -> str:
    """'approve' -> 'an approve', 'reject' -> 'a reject'. Small thing, but this
    string is read by a reviewer mid-decision, and 'a approve' undermines every
    other claim the page makes about rigour."""
    words = decision.replace("_", " ")
    return ("an " if words[:1].lower() in "aeiou" else "a ") + words


def _num(v: Any) -> float | None:
    try:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def validate(body: dict, *, claimed_amount: float, ai_lean: str) -> dict:
    """Check a submitted review against the decision model.

    Returns {"ok": bool, "errors": {field: message}, "clean": {...}} and never
    raises -- a reviewer form deserves every problem at once, not the first one
    the parser tripped over.

    `claimed_amount` and `ai_lean` come from the real claim record, so the
    arithmetic and the override gate are checked against what was actually
    submitted and actually recommended -- not against anything the client sent
    us and could have edited.
    """
    errors: dict[str, str] = {}

    decision = (body.get("decision") or "").strip().lower()
    if not decision:
        errors["decision"] = "Choose a decision."
    elif decision not in DECISIONS:
        errors["decision"] = f"Unknown decision '{decision}'."

    # Reason code — must exist AND belong to this decision.
    reason_code = (body.get("reason_code") or "").strip()
    if decision in REASON_CODES:
        allowed = REASON_CODES[decision]
        if not reason_code:
            errors["reason_code"] = "Select a reason code."
        elif reason_code not in allowed:
            # Name the likely mistake rather than just refusing: a code that is
            # valid *somewhere* means the reviewer changed decision after
            # picking it, which is worth saying out loud.
            elsewhere = next(
                (d for d, codes in REASON_CODES.items() if reason_code in codes), None
            )
            errors["reason_code"] = (
                f"'{reason_code}' is {_article(elsewhere)} reason — "
                f"it cannot justify {_article(decision)}."
                if elsewhere else f"Unknown reason code '{reason_code}'."
            )

    # Partial approval — the amount has to be real, and it has to be a partial.
    approved_amount: float | None = None
    if decision == "partial_approve":
        approved_amount = _num(body.get("approved_amount"))
        if approved_amount is None:
            errors["approved_amount"] = "Enter the amount you are approving."
        elif approved_amount <= 0:
            errors["approved_amount"] = "Approved amount must be greater than zero. To pay nothing, reject the claim."
        elif claimed_amount > 0 and approved_amount > claimed_amount:
            errors["approved_amount"] = (
                f"You cannot approve more than the {claimed_amount:,.2f} claimed."
            )
        elif claimed_amount > 0 and approved_amount == claimed_amount:
            errors["approved_amount"] = (
                "That is the full claimed amount — use 'Approve in full' instead."
            )

    # Conditional approval — the condition is the entire point.
    conditions = (body.get("conditions") or "").strip()
    if decision == "approve_with_conditions" and len(conditions) < _MIN_CONDITIONS:
        errors["conditions"] = (
            f"State the condition the claimant must meet ({_MIN_CONDITIONS}+ characters)."
        )

    # Rejection — cite the clause. A rejection a claimant cannot appeal against
    # a named rule is a rejection that will be appealed against you instead.
    policy_citation = (body.get("policy_citation") or "").strip()
    if decision == "reject" and len(policy_citation) < _MIN_REJECT_CITATION:
        errors["policy_citation"] = "Cite the policy clause or limit this rejection rests on."

    # The override gate.
    notes = (body.get("notes") or "").strip()
    override = bool(decision) and decision in DECISIONS and is_override(decision, ai_lean)
    if override:
        if not body.get("override_ack"):
            errors["override_ack"] = (
                f"The committee recommended {ai_lean}. Acknowledge that you are overriding it."
            )
        if len(notes) < _MIN_OVERRIDE_JUSTIFICATION:
            errors["notes"] = (
                f"You are overriding a {ai_lean} recommendation — explain why "
                f"({_MIN_OVERRIDE_JUSTIFICATION}+ characters). This is recorded in the ledger."
            )

    reviewer = (body.get("reviewer") or "").strip()
    if not reviewer:
        errors["reviewer"] = "Identify yourself — decisions are attributed."

    if errors:
        return {"ok": False, "errors": errors, "clean": {}}

    label = REASON_CODES.get(decision, {}).get(reason_code, reason_code)
    return {
        "ok": True,
        "errors": {},
        "clean": {
            "decision": decision,
            "outcome": outcome_of(decision),
            "reason_code": reason_code,
            "reason_label": label,
            "approved_amount": approved_amount,
            "claimed_amount": claimed_amount,
            "conditions": conditions or None,
            "policy_citation": policy_citation or None,
            "notes": notes or None,
            "reviewer": reviewer,
            "is_override": override,
            "ai_lean": ai_lean,
        },
    }


def summarize(clean: dict) -> str:
    """One-line human summary of a validated decision — used as the reviewer
    note written back to Action Center and echoed into the ledger, so the
    structured record and the free-text trail tell the same story."""
    d = clean["decision"]
    cur = ""
    if d == "partial_approve":
        head = (f"Approved in part: {clean['approved_amount']:,.2f} of "
                f"{clean['claimed_amount']:,.2f}{cur}")
    elif d == "approve_with_conditions":
        head = f"Approved with conditions: {clean['conditions']}"
    elif d == "reject":
        head = f"Rejected ({clean['policy_citation']})"
    else:
        head = "Approved in full"
    parts = [head, f"Reason: {clean['reason_label']}"]
    if clean["is_override"]:
        parts.append(f"OVERRIDE of the committee's {clean['ai_lean']} recommendation")
    if clean["notes"]:
        parts.append(f"Reviewer: {clean['notes']}")
    parts.append(f"— {clean['reviewer']}")
    return " · ".join(parts)
