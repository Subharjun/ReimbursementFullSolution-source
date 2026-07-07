"""
Live wiring for the Consensus Engine -- calls the ACTUAL deployed Orchestrator
processes (real HTTP jobs against `ReimbursementIndependentSolution`) instead of
the local Python ports in agents.py. Each function starts a real job, waits for
it, and returns the real output. No local simulation happens in this module.

Owner: Subharjun. AgentHack Track 1.

Auth: reads UIPATH_ACCESS_TOKEN from the environment first (Render), falling
back to ~/.uipath/.auth for local dev (same convention as api.py).

Folder: Shared/ReimbursementFullSolution -- the single unified folder that
holds every process/agent/api/case we control (consolidated from the old
ReimbursementIndependentSolution duplicate). Override via UIPATH_FOLDER_KEY /
UIPATH_ORG_UNIT_ID env vars if redeployed elsewhere.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error

_HERE = os.path.dirname(os.path.abspath(__file__))

UIPATH_BASE_URL = os.environ.get(
    "UIPATH_BASE_URL", "https://staging.uipath.com/hackathon26_332/DefaultTenant"
)
FOLDER_KEY = os.environ.get("UIPATH_FOLDER_KEY", "a1dc9c4b-4ba4-4ac1-837d-9c6b093457a3")
ORG_UNIT_ID = os.environ.get("UIPATH_ORG_UNIT_ID", "3152226")

# Orchestrator process (release) keys inside ReimbursementFullSolution.
PROCESS_KEYS = {
    "classify": "65C0B130-2C42-42B1-8C97-6135A7D6705C",
    "fraud": "3C0B7999-6288-4B67-862B-128450F7BEC1",
    "policy": "BC8E7D73-8791-4EFE-8E2C-FBDC6B6837EC",
    "consensus": "2F0F2630-C060-4254-B5EF-D0E9DDC556D8",
}

_JOB_TIMEOUT_S = float(os.environ.get("CONSENSUS_JOB_TIMEOUT_S", "180"))
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


def _run(process: str, input_args: dict) -> dict:
    job_id = _start_job(PROCESS_KEYS[process], input_args)
    return _wait_for_job(job_id)


# --------------------------------------------------------------------------- #
# Live equivalents of agents.py's classify / fraud_screen / policy_check,
# same return shape so debate.py's transcript-building code needs no changes.
# --------------------------------------------------------------------------- #
def classify_live(claim: dict) -> dict:
    out = _run("classify", {
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
    risk = out.get("risk_score", "Low")
    factors = out.get("risk_factors", [])
    return {
        "expense_type": out.get("expense_type", "others"),
        "risk_score": risk,
        "classification_confidence": out.get("classification_confidence", "Medium"),
        "business_purpose_valid": out.get("business_purpose_valid", True),
        "high_factors": factors if risk == "High" else [],
        "medium_factors": factors if risk == "Medium" else [],
        "_raw": out,
    }


def fraud_screen_live(claim: dict, history: list[dict] | None = None) -> dict:
    out = _run("fraud", {
        "case_id": claim.get("case_id") or "CASE-LIVE",
        "employee_email": claim.get("employee_email") or "",
        "employee_name": claim.get("employee_name") or "",
        "vendor": claim.get("vendor") or "",
        "amount": _num(claim.get("amount")),
        "currency": claim.get("currency") or "USD",
        "date": claim.get("date") or "",
        "expense_type": claim.get("expense_type") or "others",
        "reason": claim.get("purpose") or claim.get("reason") or "",
        "claim_history": history or [],
    })
    out.setdefault("duplicate_detected", False)
    out.setdefault("split_claim_detected", False)
    out.setdefault("flags", [])
    return out


def policy_check_live(claim: dict, classified: dict) -> dict:
    with open(os.path.join(_HERE, "data", "mock_policy.json")) as fh:
        policy_json = fh.read()
    et = classified["expense_type"]
    cfg = json.loads(policy_json)["categories"].get(et, json.loads(policy_json)["categories"]["others"])
    out = _run("policy", {
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
    # The live workflow's output doesn't echo the category config; attach it
    # from the same policy_json we sent, so the transcript can narrate limits.
    out["spend_limit"] = cfg["spend_limit"]
    out["auto_approve_threshold"] = cfg["auto_approve_threshold"]
    out.setdefault("policy_violations", [])
    return out


def arbitrate_live(cls: dict, frd: dict, pol: dict) -> tuple[str, str, str]:
    out = _run("consensus", {
        "risk_score": cls["risk_score"],
        "classification_confidence": cls["classification_confidence"],
        "fraud_score": _num(frd.get("fraud_score", 0)),
        "fraud_recommendation": frd.get("recommendation", "approve"),
        "duplicate_detected": bool(frd.get("duplicate_detected", False)),
        "split_claim_detected": bool(frd.get("split_claim_detected", False)),
        "policy_routing": pol.get("routing_decision", "manager_review"),
    })
    recommendation = out.get("recommendation", "HITL_REVIEW")
    rationale = out.get("rationale", "")
    path = {
        "AUTO_APPROVE": "auto_approve",
        "PROCEED": "proceed",
        "HITL_REVIEW": "hitl_review",
        "REJECT": "reject",
    }.get(recommendation, "hitl_review")
    return recommendation, path, rationale


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0
