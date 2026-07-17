"""
Reconcile -- what the app SENT vs what the Case actually ADJUDICATED.

Owner: Subharjun. AgentHack Track 1.

WHY THIS EXISTS
    MirCaseClone was built for email intake and later grew a trigger. Both
    survived. The result is that a single case run carries TWO populations of
    values that nobody reconciles:

      * trigger side -- `expenseAmount`, `expenseVendor`, `expenseTypeConfirmed`,
        `riskScore`, `duplicateDetected`. This is what /api/submit sends as the
        job's InputArguments.
      * email side -- `amount`, `currency`, `expenseType`, `reason`. Produced by
        ReimbursementIntakeBot_XP reading a real message out of the inbox.

    And the stages do not split cleanly down that line -- each reads its own
    BLEND (observed live on case f2393861, 2026-07-17):

        FraudIntegrityAgent        amount/type/vendor  <- trigger   (100% trigger)
        ClassificationAgent        vendor + flags      <- trigger
                                   email body          <- email     (hybrid, no amount)
        PolicyRuleCheckWorkflow    amount/type         <- email
                                   risk/duplicate/date <- trigger    (INCOHERENT: it
                                     scores 206 medical while carrying a risk score
                                     computed for 15,000 lodging)
        StripePayoutWorkflow       amount/type         <- email     (100% email = the money)

    Every UI surface in this app -- /dashboard's debate, /admin's funnel,
    /review's dossier and its arithmetic, and the hash-chained ledger -- reads
    the TRIGGER side, because that is what `_SUBMISSIONS` and the case job's
    InputArguments hold. The money moves on the EMAIL side. So a reviewer can
    be shown a 15,000 lodging claim, approve it, and have the Case pay 206
    medical -- with the ledger recording 15,000 under a tamper-evident hash.

    That last part is the reason this module exists. An audit trail that is
    confidently and verifiably wrong is worse than no audit trail: it launders
    a mistake into evidence. Until the Case is unified, the honest move is to
    read the values the money path ACTUALLY used and say so, loudly.

HOW WE READ THE TRUTH WITHOUT MAESTRO
    We cannot ask Maestro what `vars.amount` resolved to -- the Render OAuth app
    gets 403 PIMS-150007 on pims_ and that is closed as a probable platform gap
    (handoff 6.11). We do not need to. Every stage's work runs as a CHILD JOB
    whose `ParentJobKey` is the case job key, and a child job's InputArguments
    are exactly the resolved values the Case handed it. Those are readable with
    plain `OR.Jobs` -- which the Render token already has.

    So: list children by ParentJobKey, read the money-path child's inputs, and
    that IS the adjudicated claim. No new scope, no new permission, no Maestro.

DESIGN
    Pure functions over plain dicts. No I/O, no framework types -- api.py owns
    the HTTP and the caching. Same contract as adjudication.py so the rules stay
    unit-testable without a tenant.
"""

from __future__ import annotations

# Ordered by authority. The payout job is definitive -- its inputs are the
# values that moved the money, full stop. But it does not exist yet while a
# human is still deciding, and the reviewer is exactly who needs this most, so
# we fall back to the Policy job: it reads the same email-side `vars.amount` /
# `vars.expenseType` and it has already run by the time a HITL task exists.
#
# Classification is deliberately NOT a source: it never receives an amount at
# all (verified live), so it cannot speak to the money path.
MONEY_PATH_SOURCES: tuple[tuple[str, str], ...] = (
    ("StripePayoutWorkflow", "payout"),
    ("PolicyRuleCheckWorkflow", "policy"),
)

# Child-job input field -> the canonical claim field we compare on.
_FIELD_MAP = {
    "amount": "amount",
    "currency": "currency",
    "expense_type": "expense_type",
}

# Only these carry money-path consequence. A vendor-string difference is noise
# (the trigger sets a vendor the email never mentions); an amount or a category
# difference changes what gets paid and under which policy clause.
MATERIAL_FIELDS = ("amount", "expense_type")


def adjudicated_from_children(children: list[dict]) -> dict | None:
    """Given a case's child jobs (each {release, key, id, state, inputs}),
    return the claim the MONEY PATH actually acted on, or None if no
    money-path stage has run yet.

    `children` is expected newest-last-agnostic; we pick by source authority,
    not by time, so a completed payout always wins over the earlier policy read.
    """
    for release, source in MONEY_PATH_SOURCES:
        for c in children:
            if c.get("release") != release:
                continue
            inputs = c.get("inputs") or {}
            if not inputs:
                continue
            claim = {}
            for src_field, dest in _FIELD_MAP.items():
                if src_field in inputs:
                    claim[dest] = inputs[src_field]
            if "amount" not in claim:
                continue
            claim["source"] = source
            claim["source_process"] = release
            claim["source_job_key"] = c.get("key")
            claim["definitive"] = source == "payout"
            return claim
    return None


def _norm_amount(v) -> float | None:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _norm_str(v) -> str:
    return str(v or "").strip().lower()


def _values_agree(field: str, submitted, adjudicated) -> bool:
    if field == "amount":
        a, b = _norm_amount(submitted), _norm_amount(adjudicated)
        if a is None or b is None:
            return a == b
        return a == b
    return _norm_str(submitted) == _norm_str(adjudicated)


def compare(submitted: dict, adjudicated: dict | None) -> dict:
    """Compare what the app sent against what the Case adjudicated.

    Returns a divergence record that is always safe to render:

        {
          "known": bool,       # False = no money-path stage has run yet
          "diverged": bool,    # True = the Case acted on different values
          "material": bool,    # True = a difference that changes the payout
          "source": "payout"|"policy"|None,
          "definitive": bool,  # True only when read from the payout job
          "fields": [{field, submitted, adjudicated, agrees}],
          "summary": str,
        }

    `known: False` is NOT the same as "agrees". A caller that treats an unknown
    reconciliation as agreement reintroduces exactly the bug this module exists
    to surface, so the two are kept distinct and the summary says which it is.
    """
    if not adjudicated:
        return {
            "known": False, "diverged": False, "material": False,
            "source": None, "source_process": None, "source_job_key": None,
            "definitive": False, "fields": [],
            "summary": "No money-path stage has run yet — the values the Case "
                       "will act on are not yet observable.",
        }

    fields = []
    for f in _FIELD_MAP.values():
        if f not in adjudicated:
            continue
        sub_v = submitted.get(f)
        adj_v = adjudicated.get(f)
        fields.append({
            "field": f,
            "submitted": sub_v,
            "adjudicated": adj_v,
            "agrees": _values_agree(f, sub_v, adj_v),
        })

    diverged = any(not f["agrees"] for f in fields)
    material = any(
        not f["agrees"] for f in fields if f["field"] in MATERIAL_FIELDS
    )

    if not diverged:
        summary = "The Case acted on the values this app submitted."
    else:
        bits = [
            f"{f['field']}: submitted {f['submitted']!r} → Case used {f['adjudicated']!r}"
            for f in fields if not f["agrees"]
        ]
        where = ("the payout that executed" if adjudicated.get("definitive")
                 else "the policy check (payout has not run yet)")
        summary = (
            f"The Case did NOT act on this app's submitted values. Read from "
            f"{where}: " + "; ".join(bits) + ". Treat the Case's values as "
            "authoritative — they are what the money moves on."
        )

    return {
        "known": True,
        "diverged": diverged,
        "material": material,
        "source": adjudicated.get("source"),
        "source_process": adjudicated.get("source_process"),
        "source_job_key": adjudicated.get("source_job_key"),
        "definitive": bool(adjudicated.get("definitive")),
        "fields": fields,
        "summary": summary,
    }


def authoritative_amount(submitted_amount, divergence: dict) -> tuple[float, str]:
    """The amount a reviewer's arithmetic must be checked against, plus why.

    This is the load-bearing call. A partial approval is a statement about money
    that will actually move, so it has to be validated against the amount the
    Case will actually pay -- not the one this app happened to send. When they
    disagree and we can see the Case's value, the Case's value wins.

    When reconciliation is unknown we fall back to the submitted amount and say
    so, because the alternative (refusing every review until a payout exists)
    would block the HITL queue outright -- payment runs AFTER review.
    """
    if divergence.get("known") and divergence.get("diverged"):
        for f in divergence.get("fields", []):
            if f["field"] == "amount" and not f["agrees"]:
                amt = _norm_amount(f["adjudicated"])
                if amt is not None:
                    return amt, "case-adjudicated"
    return (_norm_amount(submitted_amount) or 0.0), "app-submitted"
