"""
Reimbursement Admin Dashboard — standalone FastAPI service.

Split out of `reimbursement-intake` (the public intake form's repo) so this
live-ops/consensus-debate feature can't destabilize the main submission flow
again — it was built there, then fully reverted on 2026-07-06 because the
combined app broke. This is the same, previously-validated code, running on
its own Render service against the same UiPath tenant/folder.

Connects, in one place, every "open" (non-protected) component in
Shared/ReimbursementFullSolution:
    - ReimbursementClassificationAgent, FraudIntegrityAgent,
      PolicyRuleCheckWorkflow, ConsensusArbitrationWorkflow
          -> consensus/live_agents.py runs a real 3-round debate against the
             actual deployed processes for any claim (POST /api/debate).
    - MirCaseClone (+ every other release in the folder)
          -> /api/admin/overview reads live Jobs/Releases health directly
             from Orchestrator for the whole folder, MirCaseClone included.
    - The orchestrator's own verdict (not Mir's Case) is the real decision
      path: AUTO_APPROVE/PROCEED fires StripePayoutWorkflow + NotificationAgent,
      REJECT fires RejectionNotificationAgent, HITL_REVIEW creates a real
      Action Center task (classification-approval-app) — see
      consensus/orchestrator.py for why this bypasses Mir's Case Management
      HITL step's persistent Resource-Catalog gap.
    - SubmissionConfirmationAgent
          -> POST /api/admin/test-confirmation fires a real test job so you
             can demo/verify it from the dashboard without a real submission.

Local dev:
    pip install -r requirements.txt
    uvicorn api:app --reload --port 8001

Render / production env vars — see .env.example.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import threading
import time
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import Body, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from consensus.debate import run_debate
from consensus import orchestrator

load_dotenv()

app = FastAPI(title="Reimbursement Admin Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ── Config ─────────────────────────────────────────────────────────────────

UIPATH_BASE_URL = os.getenv(
    "UIPATH_BASE_URL",
    "https://staging.uipath.com/hackathon26_332/DefaultTenant",
)

# Shared/ReimbursementFullSolution — the one folder holding every process
# this dashboard reads/acts on (MirCaseClone + all open items).
ADMIN_FOLDER_ID = os.getenv("UIPATH_ADMIN_FOLDER_ID", "3152226")

CONFIRMATION_AGENT_RELEASE_KEY = os.getenv(
    "UIPATH_CONFIRMATION_RELEASE_KEY",
    "ff4b2d54-d0f9-43b7-a21b-7ac1d90afe1a",
)

# ── Admin auth — every route below requires HTTP Basic auth ────────────────
ADMIN_USER = os.getenv("ADMIN_USER", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
_basic_auth = HTTPBasic(auto_error=False)


def require_admin(credentials: HTTPBasicCredentials | None = Depends(_basic_auth)) -> str:
    if not ADMIN_USER or not ADMIN_PASSWORD:
        raise HTTPException(500, "Admin auth not configured — set ADMIN_USER and ADMIN_PASSWORD env vars")
    ok = credentials is not None and secrets.compare_digest(
        credentials.username, ADMIN_USER
    ) and secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    if not ok:
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Basic"})
    return credentials.username


def _get_token() -> str:
    from consensus.auth import AuthError, get_access_token

    try:
        return get_access_token()
    except AuthError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


def _hdrs(token: str, folder_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-UIPATH-OrganizationUnitId": folder_id,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def _list_recent_jobs(token: str, folder_id: str, top: int = 30) -> list[dict]:
    url = (
        f"{UIPATH_BASE_URL}/orchestrator_/odata/Jobs"
        f"?$orderby=Id desc&$top={top}"
        "&$select=Id,Key,ReleaseName,State,StartTime,EndTime,Info"
    )
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=_hdrs(token, folder_id))
    if r.status_code != 200:
        raise HTTPException(502, f"Jobs list failed: {r.status_code} {r.text[:300]}")
    return r.json().get("value", [])


async def _list_releases(token: str, folder_id: str) -> list[dict]:
    url = (
        f"{UIPATH_BASE_URL}/orchestrator_/odata/Releases"
        "?$select=Id,Key,Name,ProcessVersion,IsLatestVersion"
    )
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=_hdrs(token, folder_id))
    if r.status_code != 200:
        raise HTTPException(502, f"Releases list failed: {r.status_code} {r.text[:300]}")
    return r.json().get("value", [])


# ── Live Consensus Engine debate — in-memory store (single instance) ───────

_DEBATES_LOCK = threading.Lock()
_DEBATES: list[dict] = []
_MAX_DEBATES = 30

_PENDING_HITL_LOCK = threading.Lock()
_PENDING_HITL: dict[str, dict] = {}

HITL_POLL_INTERVAL_S = float(os.getenv("HITL_POLL_INTERVAL_S", "20"))


def _build_totals(cases: list[dict]) -> dict:
    tot_ops = tot_dupe = tot_minutes = 0.0
    dupes = splits = auto = hitl = rejects = 0
    for r in cases:
        s = r["savings"]
        tot_ops += s["operational_saved_usd"]
        tot_minutes += s["minutes_saved"]
        if r["evidence"]["fraud"]["duplicate_detected"]:
            dupes += 1
            tot_dupe += s["duplicate_loss_prevented"]
        if r["evidence"]["fraud"]["split_claim_detected"]:
            splits += 1
        path = r["path"]
        auto += path in ("auto_approve", "proceed")
        hitl += path == "hitl_review"
        rejects += path == "reject"
    return {
        "cases": len(cases),
        "operational_saved_usd": round(tot_ops, 2),
        "duplicate_loss_prevented": round(tot_dupe, 2),
        "minutes_saved": round(tot_minutes, 1),
        "hours_saved": round(tot_minutes / 60.0, 1),
        "duplicates_caught": dupes,
        "splits_caught": splits,
        "auto_approved": auto,
        "sent_to_human": hitl,
        "rejected": rejects,
    }


def _run_consensus_debate(claim: dict) -> None:
    try:
        result = run_debate(claim)
    except Exception as exc:
        print(f"[consensus] debate failed for {claim.get('case_id')}: {exc}")
        return
    result["title"] = f"{claim.get('vendor') or 'Unknown vendor'} — {result['evidence']['classifier']['expense_type']}"
    result["blurb"] = f"Submitted by {claim.get('employee_name') or claim.get('employee_email') or 'unknown'}"
    result["submitted_at"] = time.time()

    action = orchestrator.act_on_verdict(claim, result)
    result["action"] = action
    if action.get("status") == "error":
        print(f"[orchestrator] action failed for {claim.get('case_id')}: {action['error']}")
    elif action.get("status") == "pending_review":
        print(f"[orchestrator] HITL task {action['task_id']} created for {claim.get('case_id')}")
        with _PENDING_HITL_LOCK:
            _PENDING_HITL[claim.get("case_id")] = {
                "task_id": action["task_id"],
                "claim": claim,
                "debate_result": result,
            }
    else:
        print(f"[orchestrator] {action.get('status')} for {claim.get('case_id')}")

    with _DEBATES_LOCK:
        _DEBATES.insert(0, result)
        del _DEBATES[_MAX_DEBATES:]
    print(f"[consensus] debate stored for {claim.get('case_id')} -> {result['recommendation']}")


def _update_stored_debate(case_id: str, action: dict) -> None:
    with _DEBATES_LOCK:
        for r in _DEBATES:
            if r.get("case_id") == case_id:
                r["action"] = action
                break


async def _hitl_poll_loop() -> None:
    while True:
        await asyncio.sleep(HITL_POLL_INTERVAL_S)
        with _PENDING_HITL_LOCK:
            pending = dict(_PENDING_HITL)
        for case_id, entry in pending.items():
            try:
                task = await asyncio.to_thread(orchestrator.get_hitl_task, entry["task_id"])
            except Exception as exc:
                print(f"[hitl-poll] failed to read task {entry['task_id']} ({case_id}): {exc}")
                continue
            if not task.get("action"):
                continue
            try:
                outcome = await asyncio.to_thread(
                    orchestrator.resolve_hitl_outcome, entry["claim"], entry["debate_result"], task
                )
            except Exception as exc:
                print(f"[hitl-poll] resolve failed for {case_id}: {exc}")
                continue
            print(f"[hitl-poll] {case_id} resolved -> {outcome.get('status')} (action={task.get('action')})")
            _update_stored_debate(case_id, {"status": outcome.get("status"), "reviewer_action": task.get("action")})
            with _PENDING_HITL_LOCK:
                _PENDING_HITL.pop(case_id, None)


@app.on_event("startup")
async def _start_hitl_poller() -> None:
    asyncio.create_task(_hitl_poll_loop())


# ── Public read routes (debate feed — no secrets exposed) ──────────────────

@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/debates")
def get_debates():
    with _DEBATES_LOCK:
        cases = list(_DEBATES)
    return {
        "generated_by": (
            "Live Consensus Engine — every case below was scored in real time by the actual "
            "deployed Orchestrator processes (ReimbursementClassificationAgent, FraudIntegrityAgent, "
            "PolicyRuleCheckWorkflow, ConsensusArbitrationWorkflow) in Shared/ReimbursementFullSolution."
        ),
        "totals": _build_totals(cases),
        "cases": cases,
    }


@app.get("/api/debates/latest")
def get_latest_debate():
    with _DEBATES_LOCK:
        return _DEBATES[0] if _DEBATES else {}


@app.get("/console")
def console_page():
    return FileResponse(str(Path(__file__).parent / "live_console.html"))


@app.post("/api/debate")
async def trigger_debate(claim: dict = Body(...), _admin: str = Depends(require_admin)):
    """Manually trigger a live debate for a demo/test claim (JSON body — see
    .env.example / README for the expected shape). Runs synchronously so the
    caller gets the full result back immediately; also stored for /console
    and /admin like a real submission would be."""
    case_id = claim.get("case_id") or str(uuid.uuid4())
    claim = {**claim, "case_id": case_id}
    await asyncio.to_thread(_run_consensus_debate, claim)
    with _DEBATES_LOCK:
        record = next((r for r in _DEBATES if r.get("case_id") == case_id), None)
    if record is None:
        raise HTTPException(502, "Debate did not complete — check server logs")
    return record


# ── Admin panel — live ops visibility + HITL actions ────────────────────────

@app.get("/api/admin/pending-hitl")
def get_pending_hitl(_admin: str = Depends(require_admin)):
    with _PENDING_HITL_LOCK:
        pending = list(_PENDING_HITL.items())
    return {
        "count": len(pending),
        "items": [
            {
                "case_id": case_id,
                "task_id": entry["task_id"],
                "vendor": entry["claim"].get("vendor"),
                "amount": entry["claim"].get("amount"),
                "currency": entry["claim"].get("currency"),
                "employee_name": entry["claim"].get("employee_name"),
                "employee_email": entry["claim"].get("employee_email"),
                "rationale": entry["debate_result"].get("rationale"),
            }
            for case_id, entry in pending
        ],
    }


@app.get("/api/admin/overview")
async def admin_overview(_admin: str = Depends(require_admin)):
    """Live health for every process in Shared/ReimbursementFullSolution —
    the 7 protected components plus every open item (MirCaseClone,
    FraudIntegrityAgent, ConsensusArbitrationWorkflow,
    SubmissionConfirmationAgent) in one place."""
    token = _get_token()
    jobs, releases = await asyncio.gather(
        _list_recent_jobs(token, ADMIN_FOLDER_ID),
        _list_releases(token, ADMIN_FOLDER_ID),
    )
    job_health: dict[str, dict[str, int]] = {}
    for j in jobs:
        name = j.get("ReleaseName", "Unknown")
        state = j.get("State", "Unknown")
        job_health.setdefault(name, {})
        job_health[name][state] = job_health[name].get(state, 0) + 1

    with _DEBATES_LOCK:
        cases = list(_DEBATES)
    with _PENDING_HITL_LOCK:
        pending_count = len(_PENDING_HITL)

    return {
        "folder_id": ADMIN_FOLDER_ID,
        "totals": _build_totals(cases),
        "pending_hitl_count": pending_count,
        "recent_jobs": jobs,
        "job_health": job_health,
        "releases": releases,
    }


@app.get("/api/audit/{case_id}")
def get_audit_record(case_id: str, _admin: str = Depends(require_admin)):
    with _DEBATES_LOCK:
        record = next((r for r in _DEBATES if r.get("case_id") == case_id), None)
    if record is None:
        raise HTTPException(404, f"No audit record for case {case_id}")
    body = json.dumps(record, indent=2, default=str)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="audit-{case_id}.json"'},
    )


@app.post("/api/admin/hitl/{case_id}/decide")
async def decide_hitl(case_id: str, body: dict = Body(...), _admin: str = Depends(require_admin)):
    action = str((body or {}).get("action", "")).strip().lower()
    if action not in ("approve", "reject"):
        raise HTTPException(400, "action must be 'approve' or 'reject'")
    notes = str((body or {}).get("notes", ""))

    with _PENDING_HITL_LOCK:
        entry = _PENDING_HITL.pop(case_id, None)
    if entry is None:
        raise HTTPException(404, f"No pending HITL task for case {case_id}")

    try:
        task = await asyncio.to_thread(
            orchestrator.decide_hitl_task, entry["task_id"], action.capitalize(), notes
        )
        outcome = await asyncio.to_thread(
            orchestrator.resolve_hitl_outcome, entry["claim"], entry["debate_result"], task
        )
    except Exception as exc:
        with _PENDING_HITL_LOCK:
            _PENDING_HITL[case_id] = entry
        raise HTTPException(502, f"Failed to complete HITL task: {exc}") from exc

    _update_stored_debate(case_id, {"status": outcome.get("status"), "reviewer_action": action.capitalize()})
    return {"status": "ok", "outcome": outcome}


@app.post("/api/admin/test-confirmation")
async def test_confirmation_agent(body: dict = Body(...), _admin: str = Depends(require_admin)):
    """Fire a real test job against SubmissionConfirmationAgent so it can be
    demoed/verified from the dashboard without a real form submission."""
    token = _get_token()
    url = f"{UIPATH_BASE_URL}/orchestrator_/odata/Jobs/UiPath.Server.Configuration.OData.StartJobs"
    payload = {
        "startInfo": {
            "ReleaseKey": CONFIRMATION_AGENT_RELEASE_KEY,
            "Strategy": "ModernJobsCount",
            "JobsCount": 1,
            "InputArguments": json.dumps(
                {
                    "employee_email": body.get("employee_email", ""),
                    "employee_name": body.get("employee_name", "Test User"),
                    "case_id": body.get("case_id") or str(uuid.uuid4()),
                    "expense_type": body.get("expense_type", "others"),
                    "vendor": body.get("vendor", "Test Vendor"),
                    "amount": body.get("amount", 1.0),
                    "currency": body.get("currency", "USD"),
                }
            ),
        }
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(url, json=payload, headers=_hdrs(token, ADMIN_FOLDER_ID))
    if r.status_code not in (200, 201):
        raise HTTPException(502, f"StartJobs failed: {r.status_code} {r.text[:300]}")
    return r.json()


@app.get("/admin")
def admin_page(_admin: str = Depends(require_admin)):
    return FileResponse(str(Path(__file__).parent / "admin.html"))


@app.get("/")
def root():
    return FileResponse(str(Path(__file__).parent / "live_console.html"))
