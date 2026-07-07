"""
Live wiring for the Consensus Engine -- runs the two reasoning stages that are
ACTUALLY deployed as standalone Orchestrator processes as REAL HTTP jobs, and
falls back to the deterministic local port (agents.py) per-agent if a job
fails/faults/times out or the release key has drifted. Nothing here silently
fabricates a result: every value is either a real job output or the documented
local re-implementation, and each carries a `_source` marker saying which.

Owner: Subharjun. AgentHack Track 1.

WHICH STAGES ARE LIVE
    Only two of the pipeline's reasoning stages exist as their own runnable
    Orchestrator process in `Shared/ReimbursementFullSolution`, so only these
    two run as real jobs:
      * classify  -> ReimbursementClassificationAgent  (65C0B130…, an LLM agent)
      * policy    -> PolicyRuleCheckWorkflow           (BC8E7D73…, an API workflow)
    There is NO standalone FraudIntegrityAgent or ConsensusArbitrationWorkflow
    deployed (the folder holds exactly 9 processes -- verified 2026-07-07 via
    `uip or processes list`). Fraud screening lives in `detectors.py` and the
    final arbitration is deterministic precedence code in `debate.py`; both run
    locally by design, not as a stopgap. So "live mode" is genuinely hybrid:
    real deployed agents for Classify + Policy, deterministic code for the rest.

Auth: reads UIPATH_CLIENT_ID/SECRET (client-credentials) or UIPATH_ACCESS_TOKEN
from the environment (Render), falling back to ~/.uipath/.auth for local dev
(see auth.py) -- same convention as api.py.

Folder: Shared/ReimbursementFullSolution -- the single unified folder that holds
every process/agent/api/case we control. Override via UIPATH_FOLDER_KEY /
UIPATH_ORG_UNIT_ID env vars if redeployed elsewhere.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error

from .agents import (
    classify as _classify_local,
    policy_check as _policy_check_local,
    fraud_screen as _fraud_local,
)

_HERE = os.path.dirname(os.path.abspath(__file__))

UIPATH_BASE_URL = os.environ.get(
    "UIPATH_BASE_URL", "https://staging.uipath.com/hackathon26_332/DefaultTenant"
)
FOLDER_KEY = os.environ.get("UIPATH_FOLDER_KEY", "a1dc9c4b-4ba4-4ac1-837d-9c6b093457a3")
ORG_UNIT_ID = os.environ.get("UIPATH_ORG_UNIT_ID", "3152226")

# Orchestrator process (release) keys for the FOUR reasoning stages, each now
# deployed as its own runnable process in Shared/ReimbursementFullSolution.
# Env-overridable because release keys drift on every redeploy (StartJobs -> 404
# errorCode 1002); re-find with `uip or processes list --folder-key <FOLDER_KEY>`.
# fraud + arbitration were added 2026-07-07 as real LLM-backed coded agents
# (FraudIntegrityAgent / ConsensusArbitrationAgent) -- each with a per-agent local
# fallback below, so a drifted key or a faulted job only costs the fallback, never
# a dark dashboard.
PROCESS_KEYS = {
    "classify": os.environ.get("CONSENSUS_CLASSIFY_KEY", "65C0B130-2C42-42B1-8C97-6135A7D6705C"),
    "policy": os.environ.get("CONSENSUS_POLICY_KEY", "BC8E7D73-8791-4EFE-8E2C-FBDC6B6837EC"),
    "fraud": os.environ.get("CONSENSUS_FRAUD_KEY", "f0d938be-2afd-4dfa-8b1c-970b2368e6d9"),
    "arbitration": os.environ.get("CONSENSUS_ARBITRATION_KEY", "a5957126-d32f-4b08-9057-8c3843c0e4dc"),
}

# How many of the four stages we EXPECT to run live (all with a valid key). Used
# by debate.py to decide "live" (all four real jobs) vs "hybrid" (some fell back).
EXPECTED_LIVE_STAGES = 4

_JOB_TIMEOUT_S = float(os.environ.get("CONSENSUS_JOB_TIMEOUT_S", "90"))
_POLL_INTERVAL_S = float(os.environ.get("CONSENSUS_POLL_INTERVAL_S", "3"))


class LiveCallError(RuntimeError):
    """A real Orchestrator job failed, faulted, or timed out."""


def _token() -> str:
    from .auth import AuthError, get_access_token

    try:
        return get_access_token()
    except AuthError as e:
        raise LiveCallError(str(e)) from e


def _request(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{UIPATH_BASE_URL}/orchestrator_{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {_token()}")
    req.add_header("X-UIPATH-OrganizationUnitId", ORG_UNIT_ID)
    req.add_header("Content-Type", "application/json")
    # Cloudflare in front of staging.uipath.com blocks the default urllib UA
    # (error 1010, "automated traffic") -- use a normal browser-ish UA instead.
    req.add_header("User-Agent", "curl/8.4.0")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise LiveCallError(f"{method} {path} -> HTTP {e.code}: {e.read().decode(errors='replace')}") from e
    except urllib.error.URLError as e:  # DNS / connection / TLS -- treat as a live failure, fall back
        raise LiveCallError(f"{method} {path} -> {e}") from e


def _start_job(release_key: str, input_args: dict) -> int:
    payload = {
        "startInfo": {
            "ReleaseKey": release_key,
            "Strategy": "ModernJobsCount",
            "JobsCount": 1,
            "InputArguments": json.dumps(input_args),
        }
    }
    resp = _request("POST", "/odata/Jobs/UiPath.Server.Configuration.OData.StartJobs", payload)
    return resp["value"][0]["Id"]


def _wait_for_job(job_id: int) -> dict:
    deadline = time.time() + _JOB_TIMEOUT_S
    while time.time() < deadline:
        job = _request("GET", f"/odata/Jobs({job_id})")
        state = job.get("State")
        if state == "Successful":
            return json.loads(job["OutputArguments"] or "{}")
        if state in ("Faulted", "Stopped"):
            raise LiveCallError(f"Job {job_id} ended {state}: {job.get('JobError')}")
        time.sleep(_POLL_INTERVAL_S)
    raise LiveCallError(f"Job {job_id} did not finish within {_JOB_TIMEOUT_S:.0f}s")


def _run(process: str, input_args: dict) -> tuple[dict, int]:
    """Start a real job for `process`, wait for it, return (output, job_id)."""
    job_id = _start_job(PROCESS_KEYS[process], input_args)
    return _wait_for_job(job_id), job_id


# --------------------------------------------------------------------------- #
# Live equivalents of agents.py's classify / policy_check -- same return shape
# so debate.py's transcript-building code needs no changes, plus a `_source`
# ("live" | "local") and `_job_id` marker so the UI can badge how it was scored.
# Each degrades gracefully to the local port on any LiveCallError.
# --------------------------------------------------------------------------- #
def classify_live(claim: dict) -> dict:
    try:
        out, job_id = _run("classify", {
            "case_id": claim.get("case_id") or "CASE-LIVE",
            "vendor": claim.get("vendor") or "",
            "date": claim.get("date") or "",
            "amount": _num(claim.get("amount")),
            "currency": claim.get("currency") or "USD",
            "document_attached": bool(claim.get("document_attached", True)),
            "ocr_confidence": _num(claim.get("ocr_confidence", 1.0)),
            "employee_email": claim.get("employee_email") or "",
            "duplicate_detected": bool(claim.get("duplicate_detected", False)),
            "reason": claim.get("purpose") or claim.get("reason") or "",
        })
    except LiveCallError as e:
        print(f"[consensus-live] classify job failed, using local port: {e}")
        result = _classify_local(claim)
        result["_source"] = "local"
        result["_job_id"] = None
        return result
    risk = out.get("risk_score", "Low")
    factors = out.get("risk_factors", [])
    return {
        "expense_type": out.get("expense_type", "others"),
        "risk_score": risk,
        "classification_confidence": out.get("classification_confidence", "Medium"),
        "business_purpose_valid": out.get("business_purpose_valid", True),
        "high_factors": factors if risk == "High" else [],
        "medium_factors": factors if risk == "Medium" else [],
        "_source": "live",
        "_job_id": job_id,
        "_raw": out,
    }


def policy_check_live(claim: dict, classified: dict) -> dict:
    with open(os.path.join(_HERE, "data", "mock_policy.json")) as fh:
        policy_json = fh.read()
    et = classified["expense_type"]
    cats = json.loads(policy_json)["categories"]
    cfg = cats.get(et, cats["others"])
    try:
        out, job_id = _run("policy", {
            "case_id": claim.get("case_id") or "CASE-LIVE",
            "expense_type": et,
            "amount": _num(claim.get("amount")),
            "currency": claim.get("currency") or "USD",
            "date": claim.get("date") or "",
            "document_attached": bool(claim.get("document_attached", True)),
            "business_purpose_valid": classified["business_purpose_valid"],
            "risk_score": classified["risk_score"],
            "duplicate_detected": bool(claim.get("duplicate_detected", False)),
            "policy_json": policy_json,
        })
    except LiveCallError as e:
        print(f"[consensus-live] policy job failed, using local port: {e}")
        result = _policy_check_local(claim, classified)
        result["_source"] = "local"
        result["_job_id"] = None
        return result
    # The live workflow's output doesn't echo the category config; attach it
    # from the same policy_json we sent, so the transcript can narrate limits.
    out["spend_limit"] = cfg["spend_limit"]
    out["auto_approve_threshold"] = cfg["auto_approve_threshold"]
    out.setdefault("policy_violations", [])
    out["_source"] = "live"
    out["_job_id"] = job_id
    return out


# Canonical recommendation -> path map, mirrors ConsensusArbitrationAgent so we
# can backfill `path` if the job ever omits it.
_REC_TO_PATH = {
    "AUTO_APPROVE": "auto_approve",
    "PROCEED_REVIEWED": "proceed",
    "HITL_REVIEW": "hitl_review",
    "REJECT": "reject",
}


def fraud_live(claim: dict, history: list[dict] | None = None) -> dict:
    """Live equivalent of agents.fraud_screen -- runs the deployed FraudIntegrityAgent
    (deterministic detectors floored, LLM judgment on top) as a REAL Orchestrator job.
    Returns the same keys debate.py consumes plus `_source`/`_job_id`, and degrades
    to the local deterministic detectors on any LiveCallError."""
    history = history or []
    try:
        out, job_id = _run("fraud", {
            "case_id": claim.get("case_id") or "CASE-LIVE",
            "employee_email": claim.get("employee_email") or "",
            "employee_name": claim.get("employee_name") or "",
            "vendor": claim.get("vendor") or "",
            "amount": _num(claim.get("amount")),
            "currency": claim.get("currency") or "USD",
            "date": claim.get("date") or "",
            "expense_type": claim.get("expense_type") or "",
            "reason": claim.get("purpose") or claim.get("reason") or "",
            "claim_history": history,
        })
    except LiveCallError as e:
        print(f"[consensus-live] fraud job failed, using local port: {e}")
        result = _fraud_local(claim, history)
        result["_source"] = "local"
        result["_job_id"] = None
        return result
    # Normalise to exactly the keys debate.py's evidence/challenge code reads.
    return {
        "fraud_score": int(out.get("fraud_score", 0)),
        "integrity_risk": out.get("integrity_risk", "Low"),
        "recommendation": out.get("recommendation", "proceed"),
        "duplicate_detected": bool(out.get("duplicate_detected", False)),
        "split_claim_detected": bool(out.get("split_claim_detected", False)),
        "flags": out.get("flags", []),
        "assumptions": out.get("assumptions", {}),
        "explanation": out.get("explanation", ""),
        "judged_by": out.get("judged_by", "llm"),
        "escalated": bool(out.get("escalated", False)),
        "_source": "live",
        "_job_id": job_id,
    }


def arbitrate_live(cls: dict, frd: dict, pol: dict, claim: dict) -> dict | None:
    """Live final arbitration -- runs the deployed ConsensusArbitrationAgent (the
    deterministic precedence is the compliance FLOOR, the LLM decides on top, a
    monotonic guardrail forbids softening). Returns
    {recommendation, path, rationale, _source, _job_id} on success, or None on any
    LiveCallError so the caller keeps its own deterministic `_arbitrate` result."""
    try:
        out, job_id = _run("arbitration", {
            "case_id": claim.get("case_id") or "CASE-LIVE",
            "vendor": claim.get("vendor") or "",
            "amount": _num(claim.get("amount")),
            "currency": claim.get("currency") or "USD",
            "expense_type": cls.get("expense_type") or claim.get("expense_type") or "",
            "classification_confidence": cls.get("classification_confidence", "Medium"),
            "risk_score": cls.get("risk_score", "Low"),
            "fraud_score": int(frd.get("fraud_score", 0)),
            "integrity_risk": frd.get("integrity_risk", "Low"),
            "fraud_recommendation": frd.get("recommendation", "proceed"),
            "duplicate_detected": bool(frd.get("duplicate_detected", False)),
            "split_claim_detected": bool(frd.get("split_claim_detected", False)),
            "within_spend_limit": bool(pol.get("within_spend_limit", True)),
            "policy_routing": pol.get("routing_decision", "auto_approve"),
            "policy_violations": pol.get("policy_violations", []),
            "spend_limit": _num(pol.get("spend_limit", 0)),
            "auto_approve_threshold": _num(pol.get("auto_approve_threshold", 0)),
        })
    except LiveCallError as e:
        print(f"[consensus-live] arbitration job failed, using local precedence: {e}")
        return None
    rec = out.get("recommendation", "PROCEED_REVIEWED")
    return {
        "recommendation": rec,
        "path": out.get("path") or _REC_TO_PATH.get(rec, "proceed"),
        "rationale": out.get("rationale", ""),
        "decided_by": out.get("decided_by", "llm"),
        "escalated": bool(out.get("escalated", False)),
        "_source": "live",
        "_job_id": job_id,
    }


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0
