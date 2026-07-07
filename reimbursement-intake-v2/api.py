"""
Reimbursement Intake API v2 — FastAPI backend

The 2.0 counterpart of `reimbursement-intake`: same form, but everything runs
against OUR OWN folder (Shared/ReimbursementFullSolution, id 3152226) instead
of Mir's original folder — triggers MirCaseClone (our own cloned Case) rather
than Mir's ReimbursementProcessCase.

Flow on submit:
    1. Upload receipt to the "Receipt" bucket (id 232381, folder 3152226 —
       created in an earlier session specifically for MirCaseClone/IntakeBot).
    2. Start MirCaseClone (real Maestro Case) via Orchestrator StartJobs.
    3. Fire SubmissionConfirmationAgent ONCE in the background — to the
       submitter — a "request received, verdict in <5 min" email via the
       tenant's real Gmail connection (not Resend, so it reaches any real
       address, not just the account owner's inbox). The manager is covered
       separately by the Resend finance/manager notification and the HITL
       watcher email, so the agent is not fired a second time for them.
    4. NotificationAgent / RejectionNotificationAgent are NOT called directly
       from here — they're stages inside MirCaseClone itself (Payout ->
       Notify, Rejection -> RejectNotify), so triggering the Case is what
       fires them once it reaches that stage. See HANDOFF.md item 19 for the
       one known gap (the Human-Review/HITL stage's Resource-Catalog issue) —
       everything up through Policy, and the Payout/Reject stages when a case
       resolves without HITL, executes for real.

Local dev:
    pip install -r requirements.txt
    uvicorn api:app --reload --port 8002

Render / production env vars — see .env.example.
"""

import asyncio
import base64
import json
import os
import re
import secrets
import threading
import time
import uuid
from pathlib import Path

import httpx
import resend
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Body, Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from consensus.debate import run_debate
from consensus import savings as _savings

load_dotenv()

app = FastAPI(title="Reimbursement Intake API v2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ── Config — everything defaults to Shared/ReimbursementFullSolution ─────────

UIPATH_BASE_URL = os.getenv(
    "UIPATH_BASE_URL",
    "https://staging.uipath.com/hackathon26_332/DefaultTenant",
)

# Shared/ReimbursementFullSolution (folder key a1dc9c4b-4ba4-4ac1-837d-9c6b093457a3)
FOLDER_ID = os.getenv("UIPATH_FOLDER_ID", "3152226")
FOLDER_KEY = os.getenv("UIPATH_FOLDER_KEY", "a1dc9c4b-4ba4-4ac1-837d-9c6b093457a3")

# "Receipt" bucket, created directly in this folder for MirCaseClone/IntakeBot.
BUCKET_ID = int(os.getenv("UIPATH_BUCKET_ID", "232381"))

# MirCaseClone — our own cloned Case (release key from `uip or processes list`).
# NOTE: this key changes whenever the Case is redeployed even under the same
# name (session gotcha) — if Case start 404s with errorCode 1002 ("process
# could not be found"), re-run `uip or processes list --folder-key
# a1dc9c4b-4ba4-4ac1-837d-9c6b093457a3` (read-only) and update this default
# (and the Render UIPATH_CASE_RELEASE_KEY env var, if set — it overrides this).
CASE_RELEASE_KEY = os.getenv(
    "UIPATH_CASE_RELEASE_KEY",
    "DC5612AA-9F70-42AE-8B75-C625C5A3E74C",
)

# Case Management entry point (the BPMN trigger). MUST be passed explicitly to
# StartJobs: a Case job with no entry point starts in Orchestrator (Pending ->
# Running) but the Case runtime never instantiates a real Case — no execution
# trail, no stages, no branching, then it silently cancels. Older releases had
# this baked into the release config so a bare StartJobs worked; the current
# DC5612AA release does NOT, so we specify it here to be release-config
# independent. Value is from MirCaseClone/content/entry-points.json.
CASE_ENTRY_POINT_PATH = os.getenv(
    "UIPATH_CASE_ENTRY_POINT_PATH",
    "/content/caseplan.json.bpmn#trigger_HVs1vR",
)

# SubmissionConfirmationAgent — same folder, sends via real Gmail connection.
CONFIRMATION_AGENT_RELEASE_KEY = os.getenv(
    "UIPATH_CONFIRMATION_RELEASE_KEY",
    "ff4b2d54-d0f9-43b7-a21b-7ac1d90afe1a",
)

# Resend — belt-and-suspenders finance/manager notification, same pattern as
# the main project. NOTE: Resend's free tier (no verified sending domain)
# only delivers to the account owner's own inbox — for any other real
# address this silently no-ops. The Gmail-based SubmissionConfirmationAgent
# above is the path that actually reaches arbitrary real submitters/managers;
# this is kept as an additional, non-blocking attempt, matching the main app.
NOTIFY_TO = os.getenv("RESEND_NOTIFY_TO", "akashgomez28@gmail.com")

# ── Admin panel auth — /admin and every /api/admin/* route require this ────
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


# ── Auth ──────────────────────────────────────────────────────────────────────

def _get_token() -> str:
    from uipath_auth import AuthError, get_access_token

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


# ── Bucket upload ─────────────────────────────────────────────────────────────

async def _upload_to_bucket(token: str, filename: str, content: bytes) -> str:
    safe_name = re.sub(r"[^\w.\-]", "_", filename)
    uri_url = (
        f"{UIPATH_BASE_URL}/orchestrator_/odata/Buckets({BUCKET_ID})"
        f"/UiPath.Server.Configuration.OData.GetWriteUri"
        f"?path={safe_name}&expiryInMinutes=30"
    )
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(uri_url, headers=_hdrs(token, FOLDER_ID))
        if r.status_code != 200:
            raise HTTPException(502, f"Bucket write URI failed: {r.status_code} {r.text[:300]}")
        put_r = await client.put(
            r.json()["Uri"],
            content=content,
            headers={
                "Content-Type": "application/octet-stream",
                "x-ms-blob-type": "BlockBlob",
            },
        )
        if put_r.status_code not in (200, 201, 204):
            raise HTTPException(502, f"Bucket PUT failed: {put_r.status_code} {put_r.text[:300]}")
    return safe_name


# ── MirCaseClone trigger ──────────────────────────────────────────────────────

async def _start_case(token: str, inputs: dict) -> str | None:
    url = f"{UIPATH_BASE_URL}/orchestrator_/odata/Jobs/UiPath.Server.Configuration.OData.StartJobs"
    body = {
        "startInfo": {
            "ReleaseKey": CASE_RELEASE_KEY,
            "Strategy": "ModernJobsCount",
            "JobsCount": 1,
            "EntryPointPath": CASE_ENTRY_POINT_PATH,
            "InputArguments": json.dumps(inputs),
        }
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(url, json=body, headers=_hdrs(token, FOLDER_ID))
    if r.status_code not in (200, 201):
        hint = " — token expired/misconfigured" if r.status_code == 401 else ""
        raise HTTPException(502, f"Case start failed ({r.status_code}){hint}: {r.text[:400]}")
    data = r.json()
    jobs = data.get("value", [data])
    key = jobs[0].get("Key") or jobs[0].get("Id") if jobs else None
    return str(key) if key else None


# ── Manager HITL notification — real Gmail send when a case actually needs a
# human decision, with a direct Action Center link the manager can act on ──
# from their phone/laptop. Uses the same real Gmail Integration Service
# connection the coded agents use (SubmissionConfirmationAgent /
# NotificationAgent / RejectionNotificationAgent), called directly via REST
# so no new Orchestrator job/deploy is needed for this.

GMAIL_CONNECTION_ID = os.getenv(
    "REIMBURSEMENT_GMAIL_CONNECTION_ID", "9291e875-b63f-4d6b-aaf0-84b81f41aa14"
)

# The HITL Approve/Reject decision always goes to this fixed address, regardless
# of whatever the submitter typed into the form's manager-email field — this is
# the actual reviewer for every case, not a per-submission value.
HITL_DECISION_EMAIL = os.getenv("HITL_DECISION_EMAIL", "subharjun.bose2805@gmail.com")

# How long / how often to watch a case for its HITL task to appear before
# giving up. Matches the ~5-6 min typical MirCaseClone runtime observed in
# smoke tests (Intake -> IDP -> Classify -> Policy -> HITL).
_HITL_WATCH_TIMEOUT_S = 480
_HITL_WATCH_INTERVAL_S = 8


async def _send_gmail(to: str, subject: str, body_html: str, folder_key: str = "a1dc9c4b-4ba4-4ac1-837d-9c6b093457a3") -> str:
    """Send (not draft) an email via the tenant's real Gmail IS connection.
    Returns the Gmail message id, or '' on failure. Never raises."""
    to = to.strip()
    if not to:
        return ""
    token = _get_token()
    url = f"{UIPATH_BASE_URL}/elements_/v3/element/instances/{GMAIL_CONNECTION_ID}/SendEmail"
    body_fields = {"To": to, "Subject": subject, "Body": body_html, "Importance": "normal"}
    files = {"body": ("", json.dumps(body_fields), "application/json")}
    headers = {
        "Authorization": f"Bearer {token}",
        "x-uipath-folderkey": folder_key,
        "x-uipath-originator": "saas-agents",
        "x-uipath-source": "saas-agents",
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, headers=headers, files=files)
        if r.status_code != 200:
            print(f"[gmail] SendEmail failed ({r.status_code}) to {to}: {r.text[:300]}")
            return ""
        # Real sends return a flat {"id": ..., "threadId": ..., "labelIds": ["SENT"]};
        # drafts return it nested under {"message": {"id": ...}}. Check both.
        payload = r.json()
        return payload.get("id") or payload.get("message", {}).get("id", "")
    except Exception as exc:
        print(f"[gmail] SendEmail error (non-fatal) to {to}: {exc}")
        return ""


async def _get_element_executions(instance_id: str, folder_key: str) -> dict | None:
    url = f"{UIPATH_BASE_URL}/pims_/api/v1/instances/{instance_id}/element-executions"
    try:
        token = _get_token()
        headers = {"Authorization": f"Bearer {token}", "x-uipath-folderkey": folder_key, "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url, headers=headers)
        if r.status_code != 200:
            print(f"[hitl-watch] element-executions poll failed ({r.status_code}) for {instance_id}: {r.text[:200]}")
            return None
        return r.json()
    except Exception as exc:
        print(f"[hitl-watch] element-executions poll error (non-fatal) for {instance_id}: {exc}")
        return None


async def _watch_and_notify_manager_of_hitl(
    instance_id: str | None,
    manager_email: str,
    employee_name: str,
    case_id: str,
    expense_type: str,
    vendor: str,
    amount: float,
    currency: str,
    folder_key: str = "a1dc9c4b-4ba4-4ac1-837d-9c6b093457a3",
) -> None:
    """Background task: poll the running MirCaseClone instance until its
    Human-Review UserTask shows up, then email the manager a direct Action
    Center link so they can Approve/Reject from their phone or laptop —
    best-effort, never raises, no-ops quietly if there's nothing to watch."""
    manager_email = (manager_email or "").strip()
    if not manager_email or not instance_id:
        return

    seen_element_ids: set[str] = set()
    elapsed = 0
    while elapsed < _HITL_WATCH_TIMEOUT_S:
        data = await _get_element_executions(instance_id, folder_key)
        if data:
            for el in data.get("elementExecutions", []):
                if el.get("elementType") != "UserTask":
                    continue
                el_id = el.get("elementId")
                if el_id in seen_element_ids:
                    continue
                seen_element_ids.add(el_id)
                link = el.get("externalLink") or ""
                if not link:
                    continue
                subject = f"Action needed: {expense_type} reimbursement for {employee_name} ({currency} {amount:.2f})"
                body_html = (
                    f"<p>Hi,</p>"
                    f"<p><b>{employee_name}</b> submitted a {expense_type.lower()} reimbursement "
                    f"for <b>{currency} {amount:.2f}</b> (vendor: {vendor}) that needs your review.</p>"
                    f"<p><a href=\"{link}\">Open the review task in Action Center</a> to Approve or Reject "
                    f"— works from your phone or laptop browser.</p>"
                    f"<p style=\"color:#6b7280;font-size:12px;\">Case reference: {case_id}</p>"
                )
                msg_id = await _send_gmail(manager_email, subject, body_html, folder_key)
                print(f"[hitl-watch] notified manager {manager_email} for case {case_id} (gmail id={msg_id})")
                return  # one HITL task per case run is enough
            # If the instance run is done and no UserTask ever appeared, stop watching.
            if data.get("status") in ("Completed", "Faulted", "Cancelled"):
                return
        await asyncio.sleep(_HITL_WATCH_INTERVAL_S)
        elapsed += _HITL_WATCH_INTERVAL_S
    print(f"[hitl-watch] gave up watching case {case_id} for a HITL task after {_HITL_WATCH_TIMEOUT_S}s")


# ── SubmissionConfirmationAgent — fired once, for the submitter ─────────────

async def _send_confirmation_agent(
    recipient_email: str,
    recipient_name: str,
    case_id: str,
    expense_type: str,
    vendor: str,
    amount: float,
    currency: str,
    role: str,
) -> None:
    """Fire the real SubmissionConfirmationAgent — best-effort, never raises.
    `role` is only used for logging ("submitter" / "manager"); the agent's
    email content is the same "request received, verdict in <5 min" note for
    both, since it's a generic real-Gmail confirmation, not a per-role template.
    """
    if not recipient_email.strip():
        print(f"[confirmation-agent] no {role} email provided — skipping")
        return
    try:
        token = _get_token()
        url = f"{UIPATH_BASE_URL}/orchestrator_/odata/Jobs/UiPath.Server.Configuration.OData.StartJobs"
        body = {
            "startInfo": {
                "ReleaseKey": CONFIRMATION_AGENT_RELEASE_KEY,
                "Strategy": "ModernJobsCount",
                "JobsCount": 1,
                "InputArguments": json.dumps(
                    {
                        "employee_email": recipient_email.strip(),
                        "employee_name": recipient_name,
                        "case_id": case_id,
                        "expense_type": expense_type,
                        "vendor": vendor,
                        "amount": amount,
                        "currency": currency,
                    }
                ),
            }
        }
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(url, json=body, headers=_hdrs(token, FOLDER_ID))
        if r.status_code not in (200, 201):
            print(f"[confirmation-agent] StartJobs failed for {role} ({r.status_code}): {r.text[:300]}")
            return
        print(f"[confirmation-agent] fired for {role} ({recipient_email}, case {case_id})")
    except Exception as exc:
        print(f"[confirmation-agent] error for {role} (non-fatal): {exc}")


# ── Admin panel — live Orchestrator reads (whole ReimbursementFullSolution) ─

async def _list_recent_jobs(token: str, folder_id: str, top: int = 30) -> list[dict]:
    url = (
        f"{UIPATH_BASE_URL}/orchestrator_/odata/Jobs"
        f"?$orderby=Id desc&$top={top}"
        "&$select=Id,Key,ReleaseName,State,StartTime,EndTime,CreationTime,InputArguments,Info"
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


async def _list_pending_hitl_tasks(token: str) -> tuple[list[dict], str]:
    """Real pending Action Center tasks — replaces the old always-empty
    stub. Uses GetTasksAcrossFolders (the same cross-folder endpoint
    Action Center's own UI reads from): the plain `Tasks` OData collection
    scoped by a single folder header returns an empty set no matter which
    folder is passed (row-level security appears to restrict it to tasks
    already assigned to the caller) — verified directly against the tenant.
    GetTasksAcrossFolders also conveniently returns each task's real
    OrganizationUnitId, so no folder-guessing is needed for /decide either.

    Returns (items, error) — error is "" on success. Task Management is a
    DIFFERENT Orchestrator scope category than Jobs/Releases (which needed
    OR.Jobs / OR.Execution respectively — discovered live via the admin
    panel's error banner), so this call may 403 on a scope this app's OAuth
    token doesn't have (likely OR.Tasks) even after that fix. Surfacing the
    error explicitly here (rather than swallowing it into an empty list)
    is the only way to tell "genuinely empty queue" apart from "this call
    is silently failing" from the admin UI."""
    url = (
        f"{UIPATH_BASE_URL}/orchestrator_/odata/Tasks/UiPath.Server.Configuration.OData.GetTasksAcrossFolders"
        "?$filter=Type eq 'AppTask' and IsCompleted eq false"
        "&$orderby=Id desc&$top=50"
        "&$select=Id,Title,Status,CreationTime,CreatorJobKey,OrganizationUnitId"
    )
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url, headers=headers)
    except Exception as exc:
        err = f"GetTasksAcrossFolders error: {exc}"
        print(f"[admin] {err}")
        return [], err
    if r.status_code != 200:
        err = f"GetTasksAcrossFolders failed ({r.status_code}): {r.text[:300]}"
        print(f"[admin] {err}")
        return [], err

    items: list[dict] = []
    for t in r.json().get("value", []):
        tid = t["Id"]
        _TASK_FOLDER[tid] = str(t.get("OrganizationUnitId", ""))
        job_key = t.get("CreatorJobKey") or ""
        sub = _SUBMISSIONS.get(job_key, {})
        debate_rationale = ""
        if sub.get("case_id"):
            with _DEBATES_LOCK:
                rec = next((d for d in _DEBATES if d.get("case_id") == sub["case_id"]), None)
            if rec:
                debate_rationale = rec.get("rationale", "")
        items.append({
            "task_id": tid,
            "case_id": sub.get("case_id", job_key),
            "vendor": sub.get("vendor", ""),
            "amount": sub.get("amount", 0),
            "currency": sub.get("currency", ""),
            "employee_name": sub.get("employee_name", ""),
            "employee_email": sub.get("employee_email", ""),
            "rationale": debate_rationale or f"Task: {t.get('Title', '')} (submitted outside this app instance — no local record)",
            "created_at": t.get("CreationTime"),
        })
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return items, ""


async def _decide_hitl_task(token: str, task_id: int, action: str, notes: str) -> None:
    """Real Approve/Reject against Action Center — AssignTasks then
    CompleteAppTask, same call shape validated manually against this exact
    app/case. Raises HTTPException on failure."""
    folder_id = _TASK_FOLDER.get(task_id)
    if not folder_id:
        # Not seen by a recent /pending-hitl poll — refresh once to learn its folder.
        await _list_pending_hitl_tasks(token)
        folder_id = _TASK_FOLDER.get(task_id)
    if not folder_id:
        raise HTTPException(404, f"Task {task_id} not found in any known folder — refresh and retry")

    outcome = "Approve" if action == "approve" else "Reject"
    headers = _hdrs(token, folder_id)
    async with httpx.AsyncClient(timeout=30) as client:
        # AssignTasks failing (e.g. already assigned) isn't fatal — CompleteAppTask below is the real check.
        await client.post(
            f"{UIPATH_BASE_URL}/orchestrator_/odata/Tasks/UiPath.Server.Configuration.OData.AssignTasks",
            headers=headers,
            json={"taskAssignments": [{"TaskId": task_id, "UserNameOrEmail": HITL_DECISION_EMAIL}]},
        )
        complete_r = await client.post(
            f"{UIPATH_BASE_URL}/orchestrator_/tasks/AppTasks/CompleteAppTask",
            headers=headers,
            json={"taskId": task_id, "action": outcome, "data": {"reviewerNotes": notes}},
        )
    if complete_r.status_code != 204:
        raise HTTPException(
            502, f"CompleteAppTask failed for task {task_id} (folder {folder_id}): "
            f"{complete_r.status_code} {complete_r.text[:300]}"
        )


# ── Live Consensus Engine debate — OBSERVATIONAL ONLY ───────────────────────
# MirCaseClone (triggered above) is the real decision path here — it owns
# Payout/Notify/Reject via its own stages. Running the debate too (without
# also calling consensus.orchestrator.act_on_verdict) gives /dashboard a
# real multi-agent read on every submission without double-firing payout,
# notify, or rejection actions against the same claim.

_DEBATES_LOCK = threading.Lock()
_DEBATES: list[dict] = []
_MAX_DEBATES = 30

# Raw claim dicts (oldest->newest) behind the debates, kept so each new debate
# can screen for duplicates against the real prior-claim history — that is what
# makes the "duplicate caught" verdict genuine rather than assumed.
_DEBATE_CLAIM_HISTORY: list[dict] = []
_DEBATES_BACKFILLED = False

_PENDING_HITL_LOCK = threading.Lock()
# Populated at submit time (job_id -> claim details) so a pending Action
# Center task (which only carries a CreatorJobKey) can be matched back to
# the vendor/amount/employee that submitted it, for the admin panel.
_SUBMISSIONS: dict[str, dict] = {}
_MAX_SUBMISSIONS = 200
# TaskId -> folder id it was found in, so /decide knows which folder header
# to use for AssignTasks/CompleteAppTask without re-searching.
_TASK_FOLDER: dict[int, str] = {}

# Background asyncio tasks not routed through Starlette's BackgroundTasks —
# BackgroundTasks awaits its queue SEQUENTIALLY, so a long-running poller
# (the HITL watcher, up to 480s) registered there would block every task
# queued after it (submitter/manager confirmation emails, Resend, debate)
# for its entire runtime. Fire-and-forget via asyncio.create_task() instead,
# holding a strong reference here so the task isn't garbage-collected mid-run
# (same GC-drop bug this project hit once before with a bare create_task()).
_BACKGROUND_ASYNCIO_TASKS: set[asyncio.Task] = set()


def _fire_and_forget(coro) -> None:
    task = asyncio.create_task(coro)
    _BACKGROUND_ASYNCIO_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_ASYNCIO_TASKS.discard)


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


# ── Live ops metrics from real, persistent Orchestrator job history ──────────
# A "claim" flows: MirCaseClone (the case) -> Classify/Policy/Fraud -> HITL
# (maybe) -> StripePayoutWorkflow (approved+paid) OR RejectionNotificationAgent
# (rejected). Deriving the KPI funnel from actual job history (not the
# in-memory debate list, which only covers claims submitted during THIS server
# process and resets on every Render redeploy) makes the tiles real, dynamic,
# and restart-proof. Dollar/fraud figures are only ever taken from a debate
# that actually flagged something — never fabricated.
_CASE_PROCESS = "MirCaseClone"
_PAYOUT_PROCESS = "StripePayoutWorkflow"
_REJECT_PROCESS = "RejectionNotificationAgent"

_STATE_TO_VERDICT = {
    "Successful": "completed",
    "Faulted": "faulted",
    "Stopped": "stopped",
    "Stopping": "stopped",
    "Terminating": "stopped",
    "Running": "in_progress",
    "Pending": "in_progress",
    "Resumed": "in_progress",
    "Suspended": "hitl_review",
}


# The Jobs LIST projection strips InputArguments (returns null), even with an
# explicit $select or a single-key $filter — only the by-numeric-Id single-
# entity GET returns them. Input args never change once a job is created, so
# cache them forever: the audit log enriches at most _AUDIT_ENRICH_MAX rows per
# poll, and after the first burst every row is a cache hit (no extra calls).
_JOB_INPUTS: dict[str, dict] = {}
_AUDIT_ENRICH_MAX = 15


async def _fetch_job_inputs(token: str, job_id: int, key: str) -> dict:
    if key in _JOB_INPUTS:
        return _JOB_INPUTS[key]
    url = (
        f"{UIPATH_BASE_URL}/orchestrator_/odata/Jobs({job_id})"
        "?$select=Key,InputArguments"
    )
    parsed: dict = {}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url, headers=_hdrs(token, FOLDER_ID))
        if r.status_code == 200:
            parsed = json.loads(r.json().get("InputArguments") or "{}")
    except Exception:
        parsed = {}
    _JOB_INPUTS[key] = parsed
    return parsed


def _iso_to_epoch(s: str | None) -> float | None:
    if not s:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _live_totals(jobs: list[dict], pending_hitl_count: int, debate_cases: list[dict]) -> dict:
    approved_paid = sum(
        1 for j in jobs
        if j.get("ReleaseName") == _PAYOUT_PROCESS and j.get("State") == "Successful"
    )
    rejected = sum(
        1 for j in jobs
        if j.get("ReleaseName") == _REJECT_PROCESS and j.get("State") == "Successful"
    )
    awaiting = pending_hitl_count
    claims = approved_paid + rejected + awaiting

    # Economics: single source of truth = the same ROI model the debate uses.
    min_per_claim = max(
        0.0, _savings.MANUAL_MINUTES_PER_CLAIM - _savings.AUTOMATED_MINUTES_PER_CLAIM
    )
    ops_per_claim = max(0.0, _savings.MANUAL_COST_USD - _savings.AUTOMATED_COST_USD) + max(
        0.0, _savings.CHECK_COST_USD - _savings.DIGITAL_PAYOUT_COST_USD
    )

    # Fraud/duplicate dollars are real only when a debate actually caught one.
    deb = _build_totals(debate_cases)

    return {
        "cases": claims,
        "auto_approved": approved_paid,
        "rejected": rejected,
        "awaiting_review": awaiting,
        "minutes_saved": round(claims * min_per_claim, 1),
        "hours_saved": round(claims * min_per_claim / 60.0, 1),
        "operational_saved_usd": round(claims * ops_per_claim, 2),
        "duplicate_loss_prevented": deb["duplicate_loss_prevented"],
        "duplicates_caught": deb["duplicates_caught"],
        "splits_caught": deb["splits_caught"],
        "source": "orchestrator+debate",
    }


def _live_audit(
    jobs: list[dict],
    debate_cases: list[dict],
    inputs_by_key: dict[str, dict] | None = None,
    limit: int = 15,
) -> list[dict]:
    """One row per real MirCaseClone case run (newest first). Enrich with the
    debate record when present (adds the consensus verdict + a downloadable
    full audit.json); otherwise surface the live case outcome from the job."""
    inputs_by_key = inputs_by_key or {}
    deb_by_id = {r.get("case_id"): r for r in debate_cases}
    rows: list[dict] = []
    for j in jobs:
        if j.get("ReleaseName") != _CASE_PROCESS:
            continue
        key = j.get("Key") or str(j.get("Id"))
        # List projection strips InputArguments; use the per-job cache filled
        # by admin_overview, falling back to whatever the list happened to carry.
        inp = inputs_by_key.get(key)
        if inp is None:
            try:
                inp = json.loads(j.get("InputArguments") or "{}")
            except Exception:
                inp = {}
        sub = _SUBMISSIONS.get(key, {})
        vendor = sub.get("vendor") or inp.get("expenseVendor") or ""
        amount = sub.get("amount") if sub.get("amount") is not None else inp.get("expenseAmount")
        currency = sub.get("currency") or inp.get("expenseCurrency") or ""
        if vendor and amount is not None:
            title = f"{vendor} · {currency} {amount}".strip()
        else:
            title = vendor or f"Case {key[:8]}"
        deb = deb_by_id.get(sub.get("case_id")) or deb_by_id.get(key)
        rows.append({
            "case_id": (deb or {}).get("case_id") or key,
            "title": title,
            "path": (deb or {}).get("path") or _STATE_TO_VERDICT.get(j.get("State"), "in_progress"),
            "action": {"status": j.get("State")},
            "submitted_at": _iso_to_epoch(j.get("CreationTime") or j.get("StartTime")),
            "has_full_record": deb is not None,
        })
        if len(rows) >= limit:
            break
    return rows


def _claim_from_job_inputs(case_id: str, inp: dict) -> dict:
    """Reconstruct the debate claim dict from a MirCaseClone job's real
    InputArguments (same shape the intake form builds)."""
    email = inp.get("employeeEmail") or ""
    return {
        "case_id": case_id,
        "vendor": inp.get("expenseVendor") or "",
        "date": inp.get("expenseDate") or "",
        "amount": inp.get("expenseAmount"),
        "currency": inp.get("expenseCurrency") or "USD",
        "document_attached": bool(inp.get("documentAttached")),
        "ocr_confidence": inp.get("ocrConfidence", 1.0),
        "employee_email": email,
        "employee_name": email.split("@")[0] if email else "",
        "expense_type": inp.get("expenseTypeConfirmed") or "Reimbursement",
        "purpose": "",
        "duplicate_detected": bool(inp.get("duplicateDetected")),
    }


def _assemble_debate_record(result: dict, claim: dict, submitted_at: float | None) -> dict:
    result["title"] = f"{claim.get('vendor') or 'Unknown vendor'} — {result['evidence']['classifier']['expense_type']}"
    result["blurb"] = f"Submitted by {claim.get('employee_name') or claim.get('employee_email') or 'unknown'}"
    result["submitted_at"] = submitted_at if submitted_at is not None else time.time()
    result["action"] = {"status": "observed_only", "note": "MirCaseClone owns payout/notify/reject for this claim"}
    return result


async def _backfill_debates_if_empty(limit: int = 10) -> None:
    """Populate /dashboard from REAL past claims after a restart. Pulls recent
    MirCaseClone case runs from Orchestrator, rebuilds each claim from its real
    inputs, and re-scores it through the deterministic Consensus Engine (local
    ports — no per-claim Orchestrator jobs, so it's fast and reliable) passing
    the accumulated prior-claim history so genuine duplicates are caught. Runs
    once per process; live submissions keep prepending fresh debates on top."""
    global _DEBATES_BACKFILLED
    if _DEBATES_BACKFILLED:
        return
    with _DEBATES_LOCK:
        if _DEBATES:
            _DEBATES_BACKFILLED = True
            return
    try:
        token = _get_token()
        jobs = await _list_recent_jobs(token, FOLDER_ID, top=200)
    except Exception as exc:
        print(f"[debates-backfill] job fetch failed (will retry next poll): {exc}")
        return
    case_jobs = [j for j in jobs if j.get("ReleaseName") == _CASE_PROCESS][:limit]
    await asyncio.gather(*[
        _fetch_job_inputs(token, j.get("Id"), j.get("Key") or "")
        for j in case_jobs if j.get("Id") and j.get("Key")
    ])
    records: list[dict] = []
    history: list[dict] = []
    for j in reversed(case_jobs):  # oldest -> newest so history accumulates
        key = j.get("Key") or ""
        inp = _JOB_INPUTS.get(key) or {}
        if not inp or inp.get("expenseAmount") is None:
            continue
        claim = _claim_from_job_inputs(key, inp)
        try:
            result = run_debate(claim, list(history), live=False)
        except Exception as exc:
            print(f"[debates-backfill] debate failed for {key}: {exc}")
            continue
        _assemble_debate_record(result, claim, _iso_to_epoch(j.get("CreationTime") or j.get("StartTime")))
        records.append(result)
        history.append(claim)
    records.reverse()  # newest first for display
    with _DEBATES_LOCK:
        if not _DEBATES:  # don't clobber a live submission that raced in
            _DEBATES.extend(records)
            del _DEBATES[_MAX_DEBATES:]
        _DEBATE_CLAIM_HISTORY.clear()
        _DEBATE_CLAIM_HISTORY.extend(history)
    _DEBATES_BACKFILLED = True
    print(f"[debates-backfill] seeded {len(records)} real past claim(s) into /dashboard")


def _run_consensus_debate_observe(claim: dict) -> None:
    """Background task: re-score a just-submitted claim through the Consensus
    Engine for /dashboard observability — never dispatches payout/notify/reject/
    HITL (MirCaseClone already owns that). Runs HYBRID-LIVE by default: the
    Classifier and Policy stages are scored by REAL Orchestrator jobs (the same
    deployed ReimbursementClassificationAgent / PolicyRuleCheckWorkflow the Case
    uses), each with a per-agent fallback to its local port if a job faults or a
    release key drifts, so the dashboard can never go dark. Fraud + arbitration
    are deterministic code. Runs off the HTTP response path (a FastAPI
    BackgroundTask), so the ~30s of real job time never delays the submitter.
    Set CONSENSUS_OBSERVE_LIVE=0 to force the fast all-local scoring instead.
    Screens against real prior-claim history so duplicates are genuinely caught."""
    live = os.environ.get("CONSENSUS_OBSERVE_LIVE", "1") != "0"
    with _DEBATES_LOCK:
        history = list(_DEBATE_CLAIM_HISTORY)
    try:
        result = run_debate(claim, history, live=live)
    except Exception as exc:
        print(f"[consensus] debate failed for {claim.get('case_id')}: {exc}")
        return
    print(f"[consensus] {claim.get('case_id')} scored via engine="
          f"{result.get('engine', {}).get('mode', '?')} jobs={result.get('engine', {}).get('live_job_ids')}")
    _assemble_debate_record(result, claim, time.time())

    with _DEBATES_LOCK:
        _DEBATES.insert(0, result)
        del _DEBATES[_MAX_DEBATES:]
        _DEBATE_CLAIM_HISTORY.append(claim)
        del _DEBATE_CLAIM_HISTORY[:-_MAX_DEBATES]
    print(f"[consensus] debate stored for {claim.get('case_id')} -> {result['recommendation']}")


# ── Resend — finance/manager notification (belt-and-suspenders) ────────────

def _send_notification_email(
    employee_name: str,
    employee_email: str,
    manager_email: str,
    expense_type: str,
    currency: str,
    amount: float,
    date: str,
    purpose: str,
    vendor: str,
    receipt_name: str | None,
    receipt_bytes: bytes | None,
) -> None:
    """Notify finance (NOTIFY_TO) AND the manager (if given) via Resend —
    best-effort, never raises. Runs as a FastAPI BackgroundTask."""
    api_key = os.getenv("RESEND_API_KEY", "").strip()
    if not api_key:
        print("[email] RESEND_API_KEY not set — skipping finance/manager notification")
        return

    resend.api_key = api_key

    body = (
        f"Dear Finance Team,\n\n"
        f"A {expense_type.lower()} reimbursement request has been submitted for the recent official travel. "
        f"Please find the attached {expense_type.lower()} bills for your reference.\n\n"
        f"Total amount: {currency} {amount:.2f}\n"
        f"Date of expense: {date}\n"
        f"Purpose: {purpose}\n"
        f"Vendor: {vendor}\n\n"
        f"Employee name {employee_name} and employee email is {employee_email}\n\n"
        f"Kindly process the reimbursement at the earliest convenience.\n\n"
        f"Best regards,\n"
        f"Reimbursement Portal"
    )

    recipients = [NOTIFY_TO]
    if manager_email.strip() and manager_email.strip() not in recipients:
        recipients.append(manager_email.strip())

    params: resend.Emails.SendParams = {
        "from": "Reimbursement Portal <onboarding@resend.dev>",
        "to": recipients,
        "subject": f"{expense_type} Reimbursement Request — {employee_name}",
        "text": body,
    }
    if receipt_bytes and receipt_name:
        params["attachments"] = [
            {"filename": receipt_name, "content": base64.b64encode(receipt_bytes).decode()}
        ]

    print(f"[email] Sending finance/manager notification to {recipients} via Resend…")
    try:
        result = resend.Emails.send(params)
        print(f"[email] Notification sent OK — id={result.get('id')}")
    except Exception as exc:
        print(f"[email] Resend error (non-fatal): {exc}")


def _send_confirmation_email(employee_name: str, employee_email: str, expense_type: str, currency: str, amount: float) -> None:
    """Resend fallback confirmation to the submitter — best-effort, never
    raises. Subject to the same free-tier domain restriction noted above;
    the Gmail-based SubmissionConfirmationAgent is the reliable path."""
    api_key = os.getenv("RESEND_API_KEY", "").strip()
    if not api_key:
        return
    resend.api_key = api_key
    body = (
        f"Hi {employee_name},\n\n"
        f"Your {expense_type.lower()} reimbursement request for {currency} {amount:.2f} "
        f"has been received and accepted for review.\n\n"
        f"It's now being processed automatically — you'll hear back with the verdict "
        f"in under 5 minutes.\n\n"
        f"Best regards,\n"
        f"Reimbursement Portal"
    )
    params: resend.Emails.SendParams = {
        "from": "Reimbursement Portal <onboarding@resend.dev>",
        "to": [employee_email],
        "subject": "Your reimbursement request has been received",
        "text": body,
    }
    try:
        result = resend.Emails.send(params)
        print(f"[email] Resend confirmation sent OK — id={result.get('id')}")
    except Exception as exc:
        print(f"[email] Resend confirmation error (non-fatal): {exc}")


# ── API routes ────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "commit": os.getenv("RENDER_GIT_COMMIT", "unknown")[:12]}


@app.post("/api/submit")
async def submit(
    background_tasks: BackgroundTasks,
    employeeName: str = Form(...),
    employeeEmail: str = Form(...),
    managerEmail: str = Form(""),
    expenseType: str = Form(...),
    vendor: str = Form(...),
    amount: str = Form(...),
    currency: str = Form("INR"),
    date: str = Form(...),
    purpose: str = Form(...),
    receipt: UploadFile | None = File(None),
):
    try:
        amt = float(amount)
    except ValueError:
        raise HTTPException(422, "amount must be a number.")
    if amt <= 0:
        raise HTTPException(422, "amount must be greater than 0.")

    case_id = str(uuid.uuid4())
    token = _get_token()

    document_attached = False
    attachment_name: str | None = None
    receipt_raw: bytes | None = None
    receipt_original_name: str | None = None

    if receipt and receipt.filename:
        receipt_raw = await receipt.read()
        if receipt_raw:
            receipt_original_name = receipt.filename
            attachment_name = await _upload_to_bucket(token, receipt.filename, receipt_raw)
            document_attached = True

    case_inputs = {
        "employeeEmail": employeeEmail.strip(),
        "employeeManagerEmail": managerEmail.strip(),
        "expenseVendor": vendor.strip(),
        "expenseDate": date,
        "expenseAmount": amt,
        "expenseCurrency": currency,
        "expenseTypeConfirmed": expenseType,
        "riskScore": "",
        "documentAttached": document_attached,
        "ocrConfidence": 1.0,
        "duplicateDetected": False,
        "businessPurposeValid": True,
    }

    job_id = await _start_case(token, case_inputs)

    # Remember this submission's details keyed by job_id — the only thing an
    # Action Center task carries back is its CreatorJobKey, so this is how
    # the admin panel's pending-review list resolves a task to a vendor/
    # amount/employee to actually show a human something meaningful.
    if job_id:
        with _PENDING_HITL_LOCK:
            _SUBMISSIONS[job_id] = {
                "case_id": case_id,
                "vendor": vendor,
                "amount": amt,
                "currency": currency,
                "employee_name": employeeName,
                "employee_email": employeeEmail,
                "expense_type": expenseType,
            }
            if len(_SUBMISSIONS) > _MAX_SUBMISSIONS:
                oldest = next(iter(_SUBMISSIONS))
                del _SUBMISSIONS[oldest]

    # Watch for the case's Human-Review task and email the manager a direct
    # Action Center link the moment it appears, so they can Approve/Reject
    # from their phone/laptop instead of it silently sitting unassigned.
    # Always goes to HITL_DECISION_EMAIL (the real reviewer), not the form's
    # managerEmail field.
    # Fired via asyncio.create_task (NOT background_tasks.add_task) — this
    # poller runs up to 8 minutes, and BackgroundTasks awaits its queue
    # sequentially, so registering it there would delay every other email
    # below (confirmation, Resend) by however long this poll takes.
    _fire_and_forget(
        _watch_and_notify_manager_of_hitl(
            instance_id=job_id,
            manager_email=HITL_DECISION_EMAIL,
            employee_name=employeeName,
            case_id=case_id,
            expense_type=expenseType,
            vendor=vendor,
            amount=amt,
            currency=currency,
        )
    )

    # Submitter confirmation — real Gmail send, reaches any real address.
    # Fired ONCE, to the submitter only. The manager is covered by the Resend
    # finance/manager notification below and the HITL watcher email, so we do
    # NOT fire the confirmation agent a second time for the manager (that made
    # SubmissionConfirmationAgent show up as two jobs per case in Orchestrator).
    background_tasks.add_task(
        _send_confirmation_agent,
        recipient_email=employeeEmail,
        recipient_name=employeeName,
        case_id=case_id,
        expense_type=expenseType,
        vendor=vendor,
        amount=amt,
        currency=currency,
        role="submitter",
    )
    # Resend — belt-and-suspenders finance/manager notification + submitter fallback.
    background_tasks.add_task(
        _send_notification_email,
        employee_name=employeeName,
        employee_email=employeeEmail,
        manager_email=managerEmail,
        expense_type=expenseType,
        currency=currency,
        amount=amt,
        date=date,
        purpose=purpose,
        vendor=vendor,
        receipt_name=receipt_original_name,
        receipt_bytes=receipt_raw,
    )
    background_tasks.add_task(
        _send_confirmation_email,
        employee_name=employeeName,
        employee_email=employeeEmail,
        expense_type=expenseType,
        currency=currency,
        amount=amt,
    )
    # Observational live debate for /dashboard — does NOT take any real
    # action (MirCaseClone, triggered above, is the real decision path).
    background_tasks.add_task(
        _run_consensus_debate_observe,
        {
            "case_id": case_id,
            "vendor": vendor,
            "date": date,
            "amount": amt,
            "currency": currency,
            "document_attached": document_attached,
            "ocr_confidence": 1.0,
            "employee_email": employeeEmail,
            "employee_name": employeeName,
            "expense_type": expenseType,
            "purpose": purpose,
            "duplicate_detected": False,
        },
    )

    return {
        "case_id": case_id,
        "job_id": job_id,
        "attachment": attachment_name,
        "employee": employeeName,
        "amount": amt,
        "currency": currency,
    }


# ── Live debate console (public — no secrets exposed) ───────────────────────

@app.get("/api/debates")
async def get_debates():
    await _backfill_debates_if_empty()
    with _DEBATES_LOCK:
        cases = list(_DEBATES)
    return {
        "generated_by": (
            "Consensus Engine (observational) — every case below is a REAL claim pulled "
            "from Orchestrator (MirCaseClone case runs in Shared/ReimbursementFullSolution) "
            "and re-scored through the multi-agent debate. Fresh submissions are scored "
            "HYBRID-LIVE: the Classifier and Policy stages run as REAL Orchestrator jobs "
            "(the deployed ReimbursementClassificationAgent / PolicyRuleCheckWorkflow — see "
            "each card's ⚡ badge and job id), with a per-agent fallback to the deterministic "
            "local port if a job faults or a key drifts; fraud screening and arbitration are "
            "deterministic code. Duplicates are screened against real prior-claim history — no "
            "figures are hand-authored. MirCaseClone owns the actual payout/notify/reject; "
            "this is analysis only."
        ),
        "totals": _build_totals(cases),
        "cases": cases,
    }


@app.get("/api/debates/latest")
def get_latest_debate():
    with _DEBATES_LOCK:
        return _DEBATES[0] if _DEBATES else {}


# ── Proof endpoint — resolve a Consensus job id to its REAL Orchestrator record ─
# Powers the /dashboard ⚡ live badge: a judge clicks a job id and sees the real
# job's state / start / finish straight from Orchestrator — irrefutable that the
# debate ran actual jobs, and it works even for a viewer not logged into UiPath.
@app.get("/api/proof/job/{job_id}")
async def proof_job(job_id: int):
    try:
        token = _get_token()
        url = (f"{UIPATH_BASE_URL}/orchestrator_/odata/Jobs({job_id})"
               "?$select=Id,Key,State,StartTime,EndTime,ReleaseName,CreationTime,Info")
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url, headers=_hdrs(token, FOLDER_ID))
        if r.status_code != 200:
            return {"ok": False, "job_id": job_id, "error": f"HTTP {r.status_code}"}
        j = r.json()
        started, ended = j.get("StartTime"), j.get("EndTime")
        return {
            "ok": True,
            "job_id": job_id,
            "state": j.get("State"),
            "process": j.get("ReleaseName"),
            "started": started,
            "ended": ended,
            "duration_s": _duration_s(started, ended),
            # A real, working Orchestrator deep-link scoped to our folder.
            "orchestrator_url": f"{UIPATH_BASE_URL}/orchestrator_/jobs?fid={FOLDER_ID}",
        }
    except Exception as exc:
        return {"ok": False, "job_id": job_id, "error": str(exc)}


def _duration_s(start_iso: str | None, end_iso: str | None) -> float | None:
    if not start_iso or not end_iso:
        return None
    try:
        s = _iso_to_epoch(start_iso)
        e = _iso_to_epoch(end_iso)
        return round(e - s, 1) if (s and e) else None
    except Exception:
        return None


# ── Live case progress — the REAL MirCaseClone stage cursor for the intake site ─
# The success screen polls this to watch the actual Case walk its stages, driven
# entirely off Orchestrator's element-executions (read-only). Nothing hand-faked.
_CASE_STEPS = [
    ("submitted", "Submitted"),
    ("intake", "Intake & IDP"),
    ("classify", "Classification & Policy"),
    ("review", "Human Review"),
    ("payout", "Payment & Closure"),
]


@app.get("/api/case/{job_key}/progress")
async def case_progress(job_key: str):
    # Preferred: the rich Maestro element-executions trail (needs Maestro API
    # access — available to a full user token locally).
    data = await _get_element_executions(job_key, FOLDER_KEY)
    if data and data.get("elementExecutions"):
        steps = _case_progress_from_elements(data)
        top = data.get("status")
        review = next((s for s in steps if s["key"] == "review"), {})
        return {
            "job_key": job_key, "instance_status": top, "steps": steps,
            "source": "element-executions",
            "review_link": review.get("link"),
            "done": top in ("Completed", "Faulted", "Cancelled"),
        }
    # Fallback (e.g. the Render OAuth app token can't reach the Maestro API):
    # reconstruct the same milestones from the case Job's State + whether a
    # pending HITL task exists — both within OR.Jobs / OR.Tasks scope. Still
    # 100% real Orchestrator data, just coarser than the full stage trail.
    return await _case_progress_fallback(job_key)


async def _case_progress_fallback(job_key: str) -> dict:
    try:
        token = _get_token()
        url = (f"{UIPATH_BASE_URL}/orchestrator_/odata/Jobs"
               f"?$filter=Key eq {job_key}&$select=Id,State,StartTime,EndTime&$top=1")
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url, headers=_hdrs(token, FOLDER_ID))
        job = (r.json().get("value") or [{}])[0] if r.status_code == 200 else {}
        state = job.get("State")  # Pending | Running | Successful | Faulted | Stopped
        # Is this case currently parked on a human-review task?
        at_review = False
        try:
            case_id = _SUBMISSIONS.get(job_key, {}).get("case_id")
            items, _ = await _list_pending_hitl_tasks(token)
            at_review = any(it.get("case_id") in (job_key, case_id) for it in items)
        except Exception:
            pass
    except Exception as exc:
        print(f"[case-progress] fallback failed for {job_key}: {exc}")
        state, at_review = None, False

    completed = state == "Successful"
    faulted = state in ("Faulted", "Stopped")
    running = state in ("Pending", "Running")

    def stt(done, active=False):
        return "faulted" if faulted else ("done" if done else ("active" if active else "pending"))

    steps = [
        {"key": "submitted", "label": "Submitted", "status": "done"},
        {"key": "intake", "label": "Intake & IDP", "status": stt(bool(state), active=running and not at_review)},
        {"key": "classify", "label": "Classification & Policy",
         "status": stt(at_review or completed, active=running and not at_review and not completed)},
        {"key": "review", "label": "Human Review",
         "status": "done" if completed else ("active" if at_review else stt(False))},
        {"key": "payout", "label": "Payment & Closure", "status": stt(completed)},
    ]
    return {
        "job_key": job_key,
        "instance_status": {"Successful": "Completed"}.get(state, state),
        "steps": steps, "source": "job-state", "review_link": None,
        "done": completed or faulted,
    }


def _case_progress_from_elements(data: dict | None) -> list[dict]:
    """Map the noisy 80+-element execution trail down to the 5 milestones a human
    cares about, each marked done | active | pending | faulted from REAL statuses."""
    els = (data or {}).get("elementExecutions", []) or []
    top = (data or {}).get("status")
    by_type_status: dict[str, list[str]] = {}
    trigger_done = False
    stages_completed = 0
    user_task = None
    for e in els:
        et = e.get("elementType")
        st = e.get("status")
        by_type_status.setdefault(et, []).append(st)
        if e.get("elementId") == "trigger_HVs1vR" and st == "Completed":
            trigger_done = True
        if et == "CaseStage" and st == "Completed":
            stages_completed += 1
        if et == "UserTask":
            user_task = e  # the Human-Review pause point (ttvJ2FgpA)
    # Classification/Policy ran once the CaseManager + Guardrails service tasks fire.
    processing_started = any(
        e.get("elementId") in ("CaseManagerNode", "Activity_Guardrails")
        for e in els
    )
    processing_done = any(
        e.get("elementId") == "Activity_Guardrails" and e.get("status") == "Completed"
        for e in els
    ) or (user_task is not None) or stages_completed >= 1
    ut_status = (user_task or {}).get("status")
    ut_link = (user_task or {}).get("externalLink") or (user_task or {}).get("maestroLink")
    faulted = top in ("Faulted", "Cancelled")
    completed = top == "Completed"

    def st(done, active=False):
        if faulted:
            return "faulted"
        return "done" if done else ("active" if active else "pending")

    return [
        {"key": "submitted", "label": "Submitted", "status": "done"},
        {"key": "intake", "label": "Intake & IDP",
         "status": st(trigger_done or processing_started, active=bool(els) and not trigger_done)},
        {"key": "classify", "label": "Classification & Policy",
         "status": st(processing_done, active=processing_started and not processing_done)},
        {"key": "review", "label": "Human Review",
         "status": ("done" if (ut_status == "Completed" or completed)
                    else "active" if ut_status == "InProgress" else st(False)),
         "link": ut_link},
        {"key": "payout", "label": "Payment & Closure",
         "status": "done" if completed else ("active" if ut_status == "Completed" else st(False))},
    ]


@app.get("/dashboard")
def dashboard_page():
    return FileResponse(
        str(Path(__file__).parent / "dashboard.html"),
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


# ── Admin panel — live ops visibility (Basic auth) ──────────────────────────

@app.get("/api/admin/pending-hitl")
async def get_pending_hitl(_admin: str = Depends(require_admin)):
    token = _get_token()
    items, error = await _list_pending_hitl_tasks(token)
    return {"count": len(items), "items": items, "error": error}


@app.get("/api/admin/overview")
async def admin_overview(_admin: str = Depends(require_admin)):
    token = _get_token()
    # Pull a wide job window so the funnel counts reflect real cumulative
    # activity, not just the 15 rows the "Recent jobs" panel shows.
    jobs, releases, (pending, pending_error) = await asyncio.gather(
        _list_recent_jobs(token, FOLDER_ID, top=200),
        _list_releases(token, FOLDER_ID),
        _list_pending_hitl_tasks(token),
    )
    job_health: dict[str, dict[str, int]] = {}
    for j in jobs:
        name = j.get("ReleaseName", "Unknown")
        state = j.get("State", "Unknown")
        job_health.setdefault(name, {})
        job_health[name][state] = job_health[name].get(state, 0) + 1

    with _DEBATES_LOCK:
        cases = list(_DEBATES)

    # Enrich the newest MirCaseClone case runs with their real input args
    # (vendor/amount/employee) for the audit-log titles — bounded + cached, so
    # this is one small burst on a cold cache and free thereafter.
    case_jobs = [j for j in jobs if j.get("ReleaseName") == _CASE_PROCESS][:_AUDIT_ENRICH_MAX]
    to_fetch = [j for j in case_jobs if (j.get("Key") or "") not in _JOB_INPUTS]
    if to_fetch:
        await asyncio.gather(*[
            _fetch_job_inputs(token, j.get("Id"), j.get("Key") or "")
            for j in to_fetch if j.get("Id") and j.get("Key")
        ])

    return {
        "folder_id": FOLDER_ID,
        "totals": _live_totals(jobs, len(pending), cases),
        "pending_hitl_count": len(pending),
        "pending_hitl_error": pending_error,
        "recent_jobs": jobs[:30],
        "audit": _live_audit(jobs, cases, inputs_by_key=_JOB_INPUTS),
        "job_health": job_health,
        "releases": releases,
    }


@app.get("/api/audit/{case_id}")
async def get_audit_record(case_id: str, _admin: str = Depends(require_admin)):
    # Prefer the rich in-session debate record (full consensus transcript +
    # evidence + savings). Fall back to the live Orchestrator job record when
    # the row came from real job history rather than a this-session debate, so
    # every audit-log row stays downloadable across restarts.
    with _DEBATES_LOCK:
        record = next((r for r in _DEBATES if r.get("case_id") == case_id), None)
    if record is None:
        record = await _fetch_job_audit(case_id)
    if record is None:
        raise HTTPException(404, f"No audit record for case {case_id}")
    body = json.dumps(record, indent=2, default=str)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="audit-{case_id}.json"'},
    )


async def _fetch_job_audit(job_key: str) -> dict | None:
    """Build an audit record from a real Orchestrator job when there's no
    in-session debate record for it (survives restarts / covers claims
    processed before this server started)."""
    token = _get_token()
    url = (
        f"{UIPATH_BASE_URL}/orchestrator_/odata/Jobs"
        f"?$filter=Key eq {job_key}&$top=1"
        "&$select=Id,Key,ReleaseName,State,StartTime,EndTime,CreationTime,InputArguments,Info"
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, headers=_hdrs(token, FOLDER_ID))
        if r.status_code != 200:
            return None
        vals = r.json().get("value", [])
    except Exception:
        return None
    if not vals:
        return None
    j = vals[0]
    try:
        inputs = json.loads(j.get("InputArguments") or "{}")
    except Exception:
        inputs = {}
    return {
        "case_id": j.get("Key"),
        "source": "orchestrator_job",
        "process": j.get("ReleaseName"),
        "state": j.get("State"),
        "verdict": _STATE_TO_VERDICT.get(j.get("State"), "in_progress"),
        "created_at": j.get("CreationTime"),
        "started_at": j.get("StartTime"),
        "ended_at": j.get("EndTime"),
        "inputs": inputs,
        "info": j.get("Info"),
        "note": "Live case record derived from Orchestrator job history. Full "
                "consensus-debate transcript is only retained for claims "
                "submitted during the current server session.",
    }


@app.post("/api/admin/hitl/{task_id}/decide")
async def decide_hitl(task_id: int, body: dict = Body(...), _admin: str = Depends(require_admin)):
    action = (body.get("action") or "").strip().lower()
    if action not in ("approve", "reject"):
        raise HTTPException(422, "body.action must be 'approve' or 'reject'")
    notes = (body.get("notes") or "").strip()
    token = _get_token()
    await _decide_hitl_task(token, task_id, action, notes)
    return {"task_id": task_id, "action": action, "status": "decided"}


@app.get("/admin")
def admin_page(_admin: str = Depends(require_admin)):
    return FileResponse(
        str(Path(__file__).parent / "admin.html"),
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


# ── Serve React SPA (dist/ built by Dockerfile / `npm run build`) ─────────────
# Mounted LAST so /dashboard, /admin, /api/* above always take priority.

_DIST = Path(__file__).parent / "dist"
if _DIST.exists():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="spa")
