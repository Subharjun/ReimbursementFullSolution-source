"""
Consensus Engine -- a multi-agent debate over a reimbursement claim.

Owner: Subharjun. AgentHack Track 1.

WHY THIS EXISTS
    Invoice/receipt reading is commoditized. What is NOT commodity is three
    specialised agents *cross-examining each other* and reaching a defensible,
    auditable consensus -- with the dissent recorded. This engine turns the
    pipeline's three reasoning stages (Classify, Fraud, Policy) into a transparent
    debate instead of a silent sequential hand-off.

HOW IT WORKS  (fully live by default)
    The debate runs in three rounds. By default (live=True) ALL FOUR reasoning
    stages are scored by REAL Orchestrator jobs via `live_agents.py`, each with a
    per-agent graceful fallback to its local port if the job faults or the release
    key drifts:
      * Classify     -> ReimbursementClassificationAgent (LLM agent)
      * Fraud        -> FraudIntegrityAgent (deterministic detectors floored, LLM
                        judgment on top -- may escalate but never soften)
      * Policy       -> PolicyRuleCheckWorkflow (API workflow)
      * Arbitration  -> ConsensusArbitrationAgent (deterministic precedence is the
                        compliance FLOOR, LLM makes the final call above it)
    The returned `engine` block records, per stage, whether it ran `live` (+ the
    real job id) or fell back to `local`; `mode` is "live" when all four ran as
    jobs, "hybrid" when some fell back, "fallback" when all did, "local" offline.
    Set CONSENSUS_ENGINE_OFFLINE=1 (or run_debate(..., live=False)) to force the
    all-local ports (e.g. no network / no `uip login` / fast CI / bulk backfill).

      Round 1  OPENING     each agent states its independent verdict + confidence.
      Round 2  CHALLENGE   agents cross-examine: the Fraud Sentinel challenges an
                           over-confident Classifier; the Policy Arbiter raises
                           hard-rule vetoes; the Classifier concedes or defends.
      Round 3  CONSENSUS   the ConsensusArbitrationAgent's final verdict is read
                           out and any dissent is recorded.

    Compliance safety is structural, not trust-based: the fraud + arbitration
    agents each run a deterministic floor first and a monotonic server-side
    guardrail clamps the LLM so it can only ever make a verdict STRICTER, never
    softer -- a hard block (over spend limit / confirmed duplicate) can never be
    soft-overridden by an LLM. Offline, that same deterministic precedence IS the
    verdict.

ARBITRATION PRECEDENCE  (the deterministic floor; highest wins)
    1. Hard policy violation (over spend limit, or confirmed duplicate)  -> REJECT
    2. Fraud Sentinel says reject (score >= High band)                   -> REJECT
    3. Fraud says review, OR split-claim detected                       -> HITL_REVIEW
    4. Classifier confidence Low, OR policy routing manager_review       -> HITL_REVIEW
    5. everything clean & auto-approvable                               -> AUTO_APPROVE
    6. clean but above auto-approve threshold                           -> PROCEED_REVIEWED
"""

from __future__ import annotations

import json
import os
import sys

from .agents import classify, policy_check, fraud_screen, roi, _to_float
from .live_agents import (
    classify_live, policy_check_live, fraud_live, arbitrate_live, EXPECTED_LIVE_STAGES,
)
from .redteam import red_team_review, NYX

# Live by default: every claim is scored by the ACTUAL deployed Orchestrator
# processes (real HTTP jobs), not the local ports in agents.py. Set
# CONSENSUS_ENGINE_OFFLINE=1 to fall back to the local logic (e.g. no network /
# no `uip login`, or a CI environment) -- same debate structure either way.
LIVE_DEFAULT = os.environ.get("CONSENSUS_ENGINE_OFFLINE", "") != "1"

# Agent personas (id, display name, role, avatar emoji, accent).
CLASSIFIER = {"id": "classifier", "name": "Cleo", "role": "Classifier", "avatar": "\U0001F9E0", "accent": "#6366f1"}
FRAUD = {"id": "fraud", "name": "Rex", "role": "Fraud Sentinel", "avatar": "\U0001F6E1️", "accent": "#ef4444"}
POLICY = {"id": "policy", "name": "Pola", "role": "Policy Arbiter", "avatar": "⚖️", "accent": "#0ea5a4"}

_CONF_RANK = {"Low": 0, "Medium": 1, "High": 2}


def _turn(speaker, stance, text, confidence=None, round_no=0):
    return {
        "round": round_no,
        "speaker_id": speaker["id"],
        "speaker": speaker["name"],
        "role": speaker["role"],
        "avatar": speaker["avatar"],
        "accent": speaker["accent"],
        "stance": stance,  # opening | challenge | concede | defend | veto | arbitrate | agree
        "text": text,
        "confidence": confidence,
    }


def run_debate(claim: dict, history: list[dict] | None = None, live: bool | None = None) -> dict:
    """Run the full 3-round debate. Returns transcript + consensus + evidence.

    live=True (the default) scores Classify + Policy via REAL Orchestrator jobs
    (the deployed ReimbursementClassificationAgent / PolicyRuleCheckWorkflow in
    Shared/ReimbursementFullSolution), each with a per-agent local fallback;
    Fraud + arbitration are deterministic. live=False uses the all-local ports.
    """
    history = history or []
    live = LIVE_DEFAULT if live is None else live
    amt = _to_float(claim.get("amount"))
    cur = claim.get("currency") or "USD"
    vendor = claim.get("vendor") or "(unknown vendor)"

    # --- independent findings ------------------------------------------------ #
    # Fully live: all three findings stages (Classify, Fraud, Policy) run as REAL
    # Orchestrator jobs -- ReimbursementClassificationAgent, FraudIntegrityAgent,
    # PolicyRuleCheckWorkflow -- each with its own per-agent fallback to the local
    # port if the job faults / a key drifts. live=False uses the all-local ports.
    if live:
        cls = classify_live(claim)
        frd = fraud_live(claim, history)
        pol = policy_check_live(claim, cls)
    else:
        cls = classify(claim)
        frd = fraud_screen(claim, history)
        pol = policy_check(claim, cls)
    cls_original = dict(cls)  # pre-debate snapshot, for the solo-verdict comparison below

    transcript: list[dict] = []

    # ==================== ROUND 1 - OPENING POSITIONS ==================== #
    transcript.append(_turn(
        CLASSIFIER, "opening", round_no=1, confidence=cls["classification_confidence"],
        text=(
            f"Reading the receipt: this is a **{cls['expense_type']}** expense of "
            f"{amt:,.2f} {cur} to {vendor}. My preliminary risk is "
            f"**{cls['risk_score']}** "
            f"({'no adverse factors' if not cls['high_factors'] and not cls['medium_factors'] else ', '.join(cls['high_factors'] + cls['medium_factors'])}). "
            f"Confidence: {cls['classification_confidence']}."
        ),
    ))
    flag_codes = [f["code"] for f in frd["flags"]]
    transcript.append(_turn(
        FRAUD, "opening", round_no=1, confidence=frd["integrity_risk"],
        text=(
            f"Integrity scan complete. Fraud score **{frd['fraud_score']}/100** "
            f"({frd['integrity_risk']}). "
            + ("Signals: " + ", ".join(flag_codes) + "." if flag_codes else "No fraud signals against this claimant's history.")
            + f" My call: **{frd['recommendation']}**."
        ),
    ))
    transcript.append(_turn(
        POLICY, "opening", round_no=1,
        text=(
            f"Policy view for **{cls['expense_type']}**: spend limit {pol['spend_limit']:,.0f}, "
            f"auto-approve under {pol['auto_approve_threshold']:,.0f}. "
            + (f"Violations: {', '.join(pol['policy_violations'])}." if pol["policy_violations"] else "No policy violations.")
            + f" Book routing: **{pol['routing_decision']}**."
        ),
    ))

    # ==================== ROUND 2 - CROSS-EXAMINATION ==================== #
    contested = False

    # Fraud challenges an over-confident / under-rated classification.
    dup_or_split = frd["duplicate_detected"] or frd["split_claim_detected"]
    if dup_or_split and _CONF_RANK[cls["classification_confidence"]] >= 1 and cls["risk_score"] != "High":
        contested = True
        which = "a duplicate of an earlier claim" if frd["duplicate_detected"] else "a split-claim pattern across recent submissions"
        transcript.append(_turn(
            FRAUD, "challenge", round_no=2,
            text=(
                f"@Cleo I have to push back. You rated this **{cls['risk_score']}** at "
                f"**{cls['classification_confidence']}** confidence, but I'm seeing {which}. "
                f"The category may be right — the *trust* isn't. Recommend you downgrade."
            ),
        ))
        # Classifier concedes (the honest agentic move).
        transcript.append(_turn(
            CLASSIFIER, "concede", round_no=2, confidence="Low",
            text=(
                f"@Rex fair — the pattern evidence outweighs my surface read. "
                f"Conceding: dropping confidence to **Low** and escalating risk. "
                f"The classification stands ({cls['expense_type']}); the claim does not clear on my say-so."
            ),
        ))
        cls["classification_confidence"] = "Low"
        if cls["risk_score"] == "Low":
            cls["risk_score"] = "Medium"
    elif frd["flags"] and cls["risk_score"] == "Low":
        contested = True
        transcript.append(_turn(
            FRAUD, "challenge", round_no=2,
            text=(
                f"@Cleo you cleared this as Low risk, but I logged {', '.join(flag_codes)}. "
                f"Not a veto — but it deserves a second look, not silent auto-approval."
            ),
        ))
        transcript.append(_turn(
            CLASSIFIER, "defend", round_no=2, confidence=cls["classification_confidence"],
            text=(
                "@Rex noted. Those are soft signals, so I hold the category — "
                "but I won't stand in the way of a review if Policy wants one."
            ),
        ))

    # Policy raises a hard veto if a bright-line rule is broken.
    hard_veto = (not pol["within_spend_limit"]) or claim.get("duplicate_detected") or frd["duplicate_detected"]
    if not pol["within_spend_limit"]:
        contested = True
        transcript.append(_turn(
            POLICY, "veto", round_no=2,
            text=(
                f"Bright-line stop: {amt:,.2f} {cur} exceeds the {pol['spend_limit']:,.0f} "
                f"limit for {cls['expense_type']}. This cannot proceed on any agent's discretion."
            ),
        ))
    elif frd["duplicate_detected"]:
        transcript.append(_turn(
            POLICY, "veto", round_no=2,
            text=(
                "A confirmed duplicate is a hard reject under book policy — "
                "paying it would be a double payment. I'm holding the line."
            ),
        ))
    elif pol["policy_violations"]:
        transcript.append(_turn(
            POLICY, "arbitrate", round_no=2,
            text=(
                f"I'm carrying {len(pol['policy_violations'])} violation(s) into the vote "
                f"({', '.join(pol['policy_violations'])}) — enough to deny auto-approval."
            ),
        ))

    if not contested and not pol["policy_violations"]:
        transcript.append(_turn(
            POLICY, "agree", round_no=2,
            text="No challenges raised, no violations on the books. We appear to be aligned.",
        ))

    # Nyx, the Adversarial Auditor, closes the cross-examination by arguing the
    # strongest honest case AGAINST auto-approval (Groq-powered, in-process so it
    # adds no Orchestrator-job latency; deterministic heuristic if Groq is off).
    # Monotonic: its effect is capped at "get a human" -- it can never approve or
    # hard-reject on its own (applied at arbitration below).
    red = red_team_review(claim, cls, frd, pol)
    transcript.append(_turn(
        NYX, "prosecute", round_no=2, confidence=red["confidence"],
        text=(
            red["argument"]
            + ("" if red["pushes_to"] == "review" else " (I concede there's no case to force a review.)")
        ),
    ))

    # ==================== ROUND 3 - CONSENSUS ==================== #
    # The deterministic precedence is the compliance FLOOR + the offline fallback.
    # When live, the deployed ConsensusArbitrationAgent makes the final call on top
    # of that floor (its own server-side guardrail forbids ever softening below it),
    # so a hard compliance block can never be soft-overridden by the LLM.
    recommendation, path, rationale = _arbitrate(cls, frd, pol)
    arb_source, arb_job = "deterministic", None
    if live:
        arb = arbitrate_live(cls, frd, pol, claim)
        if arb:
            recommendation, path, rationale = arb["recommendation"], arb["path"], arb["rationale"]
            arb_source, arb_job = "live", arb["_job_id"]

    # Apply Nyx monotonically: the adversary can only ever ESCALATE an
    # otherwise-approvable outcome to a human review -- never soften a review/reject
    # to an approval, and never manufacture a hard reject. So the ONLY move it can
    # make is approve-family -> HITL_REVIEW.
    redteam_escalated = False
    if red["pushes_to"] == "review" and _family(recommendation) == "approve":
        recommendation, path = "HITL_REVIEW", "hitl_review"
        rationale = (
            rationale.rstrip(".")
            + f". Escalated to a human by the Adversarial Auditor ({red['focus']}) — "
            "clean on the hard rules, but not clean enough to wave straight through."
        )
        redteam_escalated = True

    # What would each agent have decided ALONE? This is the multi-agent value
    # story: an agent in isolation can be wrong; the debate is what corrects it.
    # cls_original/pol are the pre-debate reads, i.e. exactly what each agent
    # would have ruled before any cross-examination -- their genuine solo verdict.
    persona_by_id = {"classifier": CLASSIFIER, "fraud": FRAUD, "policy": POLICY}
    solo = {
        "classifier": _classifier_leaning(cls_original),
        "fraud": frd["recommendation"],
        "policy": pol["routing_decision"],
    }
    consensus_family = _family(recommendation)
    solo_families = {k: _family(v) for k, v in solo.items()}
    # Genuine dissent = an agent whose SOLO verdict would have landed in a
    # different outcome family than the consensus (i.e. it would have been wrong
    # on its own).
    dissent = [
        {"speaker": persona_by_id[k]["name"], "role": persona_by_id[k]["role"],
         "solo_verdict": solo[k], "consensus": recommendation}
        for k, fam in solo_families.items() if fam != consensus_family
    ]
    # Did the debate flip the majority's solo instinct?
    solo_approvers = sum(1 for f in solo_families.values() if f == "approve")
    debate_changed_outcome = (solo_approvers >= 2 and consensus_family != "approve")

    closing = f"**Consensus: {recommendation}.** {rationale}"
    if debate_changed_outcome:
        closing += (f" ⚠️ On their own, {solo_approvers} of 3 agents would have cleared this — "
                    f"the cross-examination is what caught it.")
    elif dissent:
        closing += f" Solo, {', '.join(d['role'] for d in dissent)} would have differed; the debate reconciled it."
    else:
        closing += " Unanimous — no agent dissented, solo or together."
    if redteam_escalated:
        closing += (f" 🕵️ The Adversarial Auditor forced a second look here — "
                    f"the hard rules cleared, but Nyx's challenge ({red['focus']}) sent it to a human.")
    transcript.append(_turn(POLICY, "arbitrate", round_no=3, text=closing))

    # --- ROI (real model). Duplicate flips it from expected-value to full amount. #
    savings = roi(claim, duplicate_detected=frd["duplicate_detected"],
                  discount_eligible=bool(claim.get("discount_eligible", False)))

    # --- how each stage was actually scored (for the UI's live/deterministic badge) #
    agents_engine = {
        "classifier": {"source": cls.get("_source", "local"), "job_id": cls.get("_job_id"),
                       "process": "ReimbursementClassificationAgent"},
        "policy": {"source": pol.get("_source", "local"), "job_id": pol.get("_job_id"),
                   "process": "PolicyRuleCheckWorkflow"},
        "fraud": {"source": frd.get("_source", "deterministic"), "job_id": frd.get("_job_id"),
                  "process": "FraudIntegrityAgent"},
        "arbitration": {"source": arb_source, "job_id": arb_job,
                        "process": "ConsensusArbitrationAgent"},
        "redteam": {"source": red["source"], "job_id": None,
                    "process": "Nyx (Groq red team)", "pushes_to": red["pushes_to"]},
    }
    live_jobs = [a["job_id"] for a in agents_engine.values() if a["source"] == "live" and a["job_id"]]
    if not live:
        mode = "local"
    elif live_jobs:
        # All four stages ran as real jobs -> "live"; some fell back -> "hybrid".
        mode = "live" if len(live_jobs) == EXPECTED_LIVE_STAGES else "hybrid"
    else:
        mode = "fallback"  # asked for live, but every job fell back to local
    engine = {"mode": mode, "live_job_ids": live_jobs, "agents": agents_engine}

    return {
        "engine": engine,
        "case_id": claim.get("case_id") or "(no id)",
        "claim": {"vendor": vendor, "amount": amt, "currency": cur,
                  "expense_type": cls["expense_type"], "date": claim.get("date")},
        "recommendation": recommendation,
        "path": path,
        "rationale": rationale,
        "unanimous": not dissent,
        "dissent": dissent,
        "solo_verdicts": solo,
        "debate_changed_outcome": debate_changed_outcome,
        "redteam": red,
        "redteam_escalated": redteam_escalated,
        "evidence": {
            "classifier": cls,
            "fraud": {k: frd[k] for k in ("fraud_score", "integrity_risk", "recommendation",
                                          "duplicate_detected", "split_claim_detected", "flags")},
            "policy": pol,
            "redteam": red,
        },
        "savings": savings,
        "transcript": transcript,
    }


# --------------------------------------------------------------------------- #
# Arbitration precedence (documented in the module docstring)
# --------------------------------------------------------------------------- #
def _arbitrate(cls, frd, pol):
    if (not pol["within_spend_limit"]) or frd["duplicate_detected"] or pol["routing_decision"] == "reject":
        why = []
        if not pol["within_spend_limit"]:
            why.append("over the category spend limit")
        if frd["duplicate_detected"]:
            why.append("a confirmed duplicate (double-payment risk)")
        if pol["routing_decision"] == "reject" and not why:
            why.append("a hard policy violation")
        return "REJECT", "reject", "Blocked by " + " and ".join(why) + "."
    if frd["recommendation"] == "reject":
        return "REJECT", "reject", f"Fraud Sentinel scored this {frd['fraud_score']}/100 (High) — too risky to pay."
    if frd["split_claim_detected"] or frd["recommendation"] == "review":
        return "HITL_REVIEW", "hitl_review", "Fraud signals warrant a human reviewer before any payout."
    if cls["classification_confidence"] == "Low" or pol["routing_decision"] == "manager_review":
        reason = "low classification confidence" if cls["classification_confidence"] == "Low" else "policy requires manager review"
        return "HITL_REVIEW", "hitl_review", f"Escalating to a human because of {reason}."
    if pol["routing_decision"] == "auto_approve":
        return "AUTO_APPROVE", "auto_approve", "Clean on all three fronts and under the auto-approve threshold — straight through to payout."
    return "PROCEED_REVIEWED", "proceed", "No violations or fraud signals; proceeds to payout after a light review."


def _family(verdict: str) -> str:
    v = (verdict or "").lower()
    if v in ("reject",):
        return "reject"
    if v in ("auto_approve", "proceed"):
        return "approve"
    return "review"


def _classifier_leaning(cls) -> str:
    if cls["risk_score"] == "High":
        return "manager_review"
    if cls["classification_confidence"] == "Low":
        return "manager_review"
    return "auto_approve"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _pretty(result: dict) -> str:
    lines = [f"\n{'='*72}", f" CASE {result['case_id']}  ->  {result['recommendation']}", f"{'='*72}"]
    for t in result["transcript"]:
        tag = f"R{t['round']} {t['avatar']} {t['speaker']} ({t['role']}) [{t['stance']}]"
        lines.append(f"\n{tag}\n    {t['text']}")
    s = result["savings"]
    lines.append(f"\n  ROI: ~{s['minutes_saved']:.0f}m saved, "
                 f"~${s['operational_saved_usd']:,.2f} ops, "
                 f"dup-prevented {s['duplicate_loss_prevented']:,.2f} {s['currency']}")
    if result["dissent"]:
        lines.append("  Solo dissent: " + "; ".join(f"{d['role']} alone would {d['solo_verdict']}" for d in result["dissent"]))
    return "\n".join(lines)


if __name__ == "__main__":
    mode = "LIVE (real Orchestrator jobs)" if LIVE_DEFAULT else "OFFLINE (local ports, CONSENSUS_ENGINE_OFFLINE=1)"
    print(f"[Consensus Engine running in {mode} mode]")
    if len(sys.argv) > 1:
        with open(sys.argv[1]) as fh:
            payload = json.load(fh)
        claim = payload.get("claim", payload)
        history = payload.get("history", [])
        print(_pretty(run_debate(claim, history)))
    else:
        from scenarios import SCENARIOS
        for sc in SCENARIOS:
            print(_pretty(run_debate(sc["claim"], sc.get("history", []))))
