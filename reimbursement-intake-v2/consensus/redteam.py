"""
Red Team -- an adversarial "prosecutor" voice in the Consensus debate (#4).

Owner: Subharjun. AgentHack Track 1.

WHY THIS EXISTS
    Cleo (Classifier), Rex (Fraud) and Pola (Policy) each form a good-faith read.
    A real audit function also needs someone whose ONLY job is to argue the other
    way -- to find the weakest point and make the strongest honest case that a
    claim should not sail through. That is Nyx, the Adversarial Auditor. It makes
    the debate a genuine debate rather than three cooperating agents nodding along.

    Nyx is powered by Groq (llama-3.3-70b) because it must be FAST -- it runs
    in-process, not as an Orchestrator job, so it never adds to the ~140s live
    debate budget. If Groq is unavailable it degrades to a deterministic heuristic
    over the same evidence, so Nyx always speaks.

THE MONOTONIC RULE (same philosophy as Fraud + Arbitration)
    Nyx can only ever make the outcome STRICTER, never softer. Concretely:
      * its maximum effect is to escalate an otherwise-approvable claim to
        HITL_REVIEW (get a human) -- it can NEVER auto-approve, and it can NEVER
        by itself force a hard REJECT (a reject requires the deterministic floor:
        over spend limit / confirmed duplicate / High fraud band).
      * it may NOT invent a signal the detectors did not find; it must argue from
        the evidence it is given.
    So Nyx can catch "this smells wrong, get a human to look" -- exactly the value
    of an adversary -- without ever being able to soft-override a compliance rule
    or block a payout on an LLM's opinion alone.
"""

from __future__ import annotations

from .groq_client import groq_json, groq_available

# Persona (matches the shape used by debate.py's other agents).
NYX = {"id": "redteam", "name": "Nyx", "role": "Adversarial Auditor",
       "avatar": "\U0001F575️", "accent": "#b91c1c"}

_ALLOWED_PUSH = {"none", "review"}


def _findings_digest(cls: dict, frd: dict, pol: dict) -> dict:
    """Compact, LLM-friendly summary of what the three agents already found.

    Deliberately only the facts Nyx is allowed to reason from -- it cannot invent
    beyond this.
    """
    return {
        "expense_type": cls.get("expense_type"),
        "classifier_risk": cls.get("risk_score"),
        "classifier_confidence": cls.get("classification_confidence"),
        "fraud_score": frd.get("fraud_score"),
        "fraud_band": frd.get("integrity_risk"),
        "fraud_recommendation": frd.get("recommendation"),
        "fraud_flags": [f.get("code") for f in frd.get("flags", [])],
        "duplicate_detected": frd.get("duplicate_detected"),
        "split_claim_detected": frd.get("split_claim_detected"),
        "within_spend_limit": pol.get("within_spend_limit"),
        "policy_violations": pol.get("policy_violations", []),
        "policy_routing": pol.get("routing_decision"),
    }


def red_team_review(claim: dict, cls: dict, frd: dict, pol: dict) -> dict:
    """Return Nyx's adversarial review.

    Shape:
      { "argument": str,           # 2-3 sentence case, cites the evidence
        "focus": str,              # the single biggest concern (short)
        "pushes_to": "none"|"review",   # its EFFECT, already clamped monotonically
        "confidence": "Low"|"Medium"|"High",
        "source": "groq"|"heuristic" }
    """
    digest = _findings_digest(cls, frd, pol)
    amt = claim.get("amount")
    cur = claim.get("currency") or "USD"
    vendor = claim.get("vendor") or "(unknown vendor)"

    system = (
        "You are Nyx, an adversarial internal auditor (a 'red team') reviewing a "
        "corporate expense reimbursement. Your ONLY job is to make the strongest "
        "HONEST case that this claim should not be auto-approved -- find its "
        "weakest point. Rules you must obey: (1) argue only from the evidence you "
        "are given; never invent a signal that isn't there. (2) The most you can "
        "ask for is a human review ('review'); you cannot approve, and you cannot "
        "block a payout on your own. (3) If the evidence is genuinely clean and "
        "there is no honest case to make, say so and set pushes_to='none' -- do "
        "not manufacture suspicion. Keep the argument to 2-3 sentences, concrete, "
        "and specific to the numbers/flags provided."
    )
    user = (
        f"Claim: {amt} {cur} to '{vendor}'.\n"
        f"Agent findings: {digest}\n\n"
        'Return JSON: {"argument": "<2-3 sentences>", "focus": "<short phrase>", '
        '"pushes_to": "none" or "review", "confidence": "Low"|"Medium"|"High"}'
    )

    out = groq_json(system, user, temperature=0.35, max_tokens=320)
    if out and isinstance(out.get("argument"), str) and out["argument"].strip():
        push = str(out.get("pushes_to", "none")).strip().lower()
        if push not in _ALLOWED_PUSH:
            push = "review" if push in ("reject", "escalate", "hitl", "hitl_review") else "none"
        conf = str(out.get("confidence", "Medium")).strip().title()
        if conf not in ("Low", "Medium", "High"):
            conf = "Medium"
        return {
            "argument": out["argument"].strip(),
            "focus": str(out.get("focus", "")).strip() or "overall trust",
            "pushes_to": _clamp_push(push, cls, frd, pol),
            "confidence": conf,
            "source": "groq",
        }

    # ---- deterministic fallback (Groq unavailable) -------------------------- #
    return _heuristic_review(digest, cls, frd, pol)


def _has_substantive_signal(cls: dict, frd: dict, pol: dict) -> bool:
    """A signal weighty enough to justify pulling in a human.

    Deliberately EXCLUDES the "low"-severity contextual flags on their own
    (WEEKEND_DATE, ROUND_AMOUNT) -- those are colour, not cause. Escalating every
    weekend-dated or round-number receipt to a human would defeat auto-approval.
    A medium/high fraud flag, a split-claim, low classifier confidence, an elevated
    risk band, a policy violation, or manager-review routing all qualify.
    """
    if frd.get("split_claim_detected"):
        return True
    if any(str(f.get("severity", "")).lower() in ("medium", "high") for f in frd.get("flags", [])):
        return True
    if cls.get("classification_confidence") == "Low":
        return True
    if cls.get("risk_score") in ("Medium", "High"):
        return True
    if pol.get("policy_violations"):
        return True
    if pol.get("routing_decision") in ("manager_review", "reject"):
        return True
    return False


def _clamp_push(push: str, cls: dict, frd: dict, pol: dict) -> str:
    """Monotonic clamp: Nyx may only escalate, and only when there is a substantive
    signal to hang the escalation on -- it cannot escalate a spotless (or
    only-weakly-flagged) claim purely on rhetoric."""
    if push != "review":
        return "none"
    return "review" if _has_substantive_signal(cls, frd, pol) else "none"


def _heuristic_review(digest: dict, cls: dict, frd: dict, pol: dict) -> dict:
    """Evidence-only adversarial read when Groq is offline. Same monotonic cap."""
    concerns: list[str] = []
    focus = "overall trust"
    if frd.get("split_claim_detected"):
        concerns.append("a split-claim pattern that would dodge the approval gate")
        focus = "split-claim avoidance"
    strong_flags = [f.get("code") for f in frd.get("flags", [])
                    if str(f.get("severity", "")).lower() in ("medium", "high")]
    if strong_flags:
        concerns.append(f"integrity flags ({', '.join(strong_flags)})")
        focus = focus if focus != "overall trust" else "integrity flags"
    if cls.get("classification_confidence") == "Low":
        concerns.append("the classifier's own low confidence in the read")
        focus = focus if focus != "overall trust" else "weak classification"
    if cls.get("risk_score") in ("Medium", "High"):
        concerns.append(f"an elevated {cls['risk_score']} risk band")
        focus = focus if focus != "overall trust" else "elevated risk"
    if pol.get("policy_violations"):
        concerns.append(f"{len(pol['policy_violations'])} policy violation(s) on the books")
        focus = "policy violations"

    weak_flags = [f.get("code") for f in frd.get("flags", [])
                  if str(f.get("severity", "")).lower() == "low"]

    if concerns:
        argument = (
            "I'm not satisfied this should auto-approve. I'm looking at "
            + "; ".join(concerns)
            + ". None of that is a hard block on its own, but together it's enough "
            "that a human should sign off rather than the system waving it through."
        )
        push = _clamp_push("review", cls, frd, pol)
        conf = "High" if (frd.get("split_claim_detected") or pol.get("policy_violations")) else "Medium"
    elif weak_flags:
        argument = (
            f"The only things I can point to are soft, contextual notes ({', '.join(weak_flags)}) — "
            "worth logging, but I can't honestly build a case to pull a human in over them alone. "
            "I'll note them and stand down."
        )
        push, conf = "none", "Low"
    else:
        argument = (
            "I went looking for a reason to hold this and couldn't find an honest "
            "one: the amount is within limit, no integrity flags, and the "
            "classification is confident. I have no case to make against it."
        )
        push, conf = "none", "Low"

    return {
        "argument": argument,
        "focus": focus,
        "pushes_to": push,
        "confidence": conf,
        "source": "heuristic",
    }
