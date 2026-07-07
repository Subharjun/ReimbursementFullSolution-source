"""
Full-control orchestrator -- acts on the live Consensus Engine's verdict for
real, entirely against processes deployed in OUR OWN folder
(Shared/ReimbursementFullSolution, id 3152226), with zero dependency on Mir's
Maestro Case or its Resource Catalog.

Owner: Subharjun. AgentHack Track 1.

WHY THIS EXISTS
    Mir's live Case (and our own MirCaseClone) can only reach the human-review
    step through Maestro Case Management's internal app-resolution, which has a
    persistent Resource Catalog bug for classification-approval-app in this
    folder (see HANDOFF.md item 19). Investigated directly against the tenant
    on 2026-07-05 and found: Orchestrator's native App Task creation endpoint
    (`POST /orchestrator_/tasks/AppTasks/CreateAppTask`) resolves the app by
    its AppId directly -- it does NOT go through `uip solution resource
    refresh` / RCS at all, so it is unaffected by that gap. Verified live:
    created + read back a real task in folder 3152226
    (organizationUnitFullyQualifiedName confirmed "Shared/ReimbursementFullSolution").

    This module makes the already-live debate (Classify + FraudIntegrityAgent +
    PolicyRuleCheckWorkflow + ConsensusArbitrationWorkflow, all real deployed
    processes) the actual decision-maker instead of an observational side
    channel: on AUTO_APPROVE/PROCEED it fires the real Stripe payout + Notify,
    on REJECT it fires the real RejectionNotify, and on HITL_REVIEW it creates
    a real Action Center task against classification-approval-app -- a human
    reviewer sees a normal Action Center approve/reject form, no different from
    the Maestro Case's own HITL step, just dispatched directly instead of
    through Case Management.

FOLDER / RELEASES (Shared/ReimbursementFullSolution, id 3152226, key a1dc9c4b-...)
    All of these are Subharjun's own deployed processes -- no Mir dependency.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error

UIPATH_BASE_URL = os.environ.get(
    "UIPATH_BASE_URL", "https://staging.uipath.com/hackathon26_332/DefaultTenant"
)

# Shared/ReimbursementFullSolution -- Subharjun's own folder, already has every
# process this orchestrator needs.
ORCH_FOLDER_ID = os.environ.get("UIPATH_ORCH_FOLDER_ID", "3152226")

RELEASE_KEYS = {
    "stripe": os.environ.get("UIPATH_STRIPE_RELEASE_KEY", "a494d4b8-1c70-4eb7-ae29-ce70895affa6"),
    "notify": os.environ.get("UIPATH_NOTIFY_RELEASE_KEY", "8ebc34be-11af-404e-8027-00da7a6380f4"),
    "reject_notify": os.environ.get(
        "UIPATH_REJECT_NOTIFY_RELEASE_KEY", "9fe85c9d-5eca-49bd-b816-ca982d4e815b"
    ),
}

# classification-approval-app, deployed + confirmed working in folder 3152226
# via direct CreateAppTask (bypasses the RCS gap Maestro Case hits).
HITL_APP_ID = os.environ.get(
    "UIPATH_HITL_APP_ID", "IDb1f8b84d2b164724ac832c5964f5dc56"
)

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "").strip()

_JOB_TIMEOUT_S = float(os.environ.get("ORCH_JOB_TIMEOUT_S", "120"))
_POLL_INTERVAL_S = float(os.environ.get("ORCH_JOB_POLL_INTERVAL_S", "3"))


class OrchestratorError(RuntimeError):
    """A real Orchestrator/Task call failed, faulted, or timed out."""


def _token() -> str:
    from .auth import AuthError, get_access_token

    try:
        return get_access_token()
    except AuthError as e:
        raise OrchestratorError(str(e)) from e


def _call(method: str, path: str, folder_id: str, body: dict | None = None) -> dict:
    url = f"{UIPATH_BASE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {_token()}")
    req.add_header("X-UIPATH-OrganizationUnitId", folder_id)
    req.add_header("Content-Type", "application/json")
    # Cloudflare in front of staging.uipath.com blocks the default urllib UA.
    req.add_header("User-Agent", "curl/8.4.0")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise OrchestratorError(f"{method} {path} -> HTTP {e.code}: {e.read().decode(errors='replace')}") from e


def _start_job(release_key: str, input_args: dict) -> int:
    payload = {
        "startInfo": {
            "ReleaseKey": release_key,
            "Strategy": "ModernJobsCount",
            "JobsCount": 1,
            "InputArguments": json.dumps(input_args),
        }
    }
    resp = _call(
        "POST",
        "/orchestrator_/odata/Jobs/UiPath.Server.Configuration.OData.StartJobs",
        ORCH_FOLDER_ID,
        payload,
    )
    return resp["value"][0]["Id"]


def _wait_for_job(job_id: int) -> dict:
    deadline = time.time() + _JOB_TIMEOUT_S
    while time.time() < deadline:
        job = _call("GET", f"/orchestrator_/odata/Jobs({job_id})", ORCH_FOLDER_ID)
        state = job.get("State")
        if state == "Successful":
            return json.loads(job["OutputArguments"] or "{}")
        if state in ("Faulted", "Stopped"):
            raise OrchestratorError(f"Job {job_id} ended {state}: {job.get('JobError')}")
        time.sleep(_POLL_INTERVAL_S)
    raise OrchestratorError(f"Job {job_id} did not finish within {_JOB_TIMEOUT_S:.0f}s")


def _run(release_key: str, input_args: dict) -> dict:
    job_id = _start_job(release_key, input_args)
    return _wait_for_job(job_id)


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------- #
# Payout + notify (AUTO_APPROVE / PROCEED)
# --------------------------------------------------------------------------- #
def fire_payout_and_notify(claim: dict, debate_result: dict) -> dict:
    cls = debate_result["evidence"]["classifier"]
    frd = debate_result["evidence"]["fraud"]
    amount = _num(claim.get("amount"))
    currency = claim.get("currency") or "USD"

    if not STRIPE_SECRET_KEY:
        payout = {
            "payout_status": "skipped_no_stripe_key",
            "payment_status": "skipped_no_stripe_key",
            "erp_system": "Stripe", "contact_id": "", "fund_account_id": "",
            "payout_id": "", "reference_id": "",
        }
    else:
        payout = _run(RELEASE_KEYS["stripe"], {
            "case_id": claim.get("case_id") or "",
            "employee_email": claim.get("employee_email") or "",
            "employee_name": claim.get("employee_name") or "",
            "amount": amount,
            "currency": currency,
            "expense_type": cls.get("expense_type") or "others",
            "reason": claim.get("purpose") or "",
            "stripe_secret_key": STRIPE_SECRET_KEY,
            "stripe_publishable_key": STRIPE_PUBLISHABLE_KEY,
        })

    notify = _run(RELEASE_KEYS["notify"], {
        "employee_email": claim.get("employee_email") or "",
        "employee_name": claim.get("employee_name") or "",
        "case_id": claim.get("case_id") or "",
        "erp_system": payout.get("erp_system", "Stripe"),
        "payout_id": payout.get("payout_id", ""),
        "contact_id": payout.get("contact_id", ""),
        "fund_account_id": payout.get("fund_account_id", ""),
        "payout_status": payout.get("payout_status", ""),
        "payment_status": payout.get("payment_status", ""),
        "amount": amount,
        "currency": currency,
        "reference_id": payout.get("reference_id", ""),
        "duplicate_detected": bool(frd.get("duplicate_detected", False)),
        "discount_eligible": False,
    })
    return {"status": "paid", "payout": payout, "notify": notify}


# --------------------------------------------------------------------------- #
# Rejection notify (REJECT)
# --------------------------------------------------------------------------- #
def fire_rejection_notify(claim: dict, debate_result: dict, reviewer_notes: str = "") -> dict:
    cls = debate_result["evidence"]["classifier"]
    out = _run(RELEASE_KEYS["reject_notify"], {
        "employee_email": claim.get("employee_email") or "",
        "employee_name": claim.get("employee_name") or "",
        "case_id": claim.get("case_id") or "",
        "expense_type": cls.get("expense_type") or "others",
        "vendor": claim.get("vendor") or "",
        "amount": _num(claim.get("amount")),
        "currency": claim.get("currency") or "USD",
        "risk_score": cls.get("risk_score") or "",
        "reviewer_notes": reviewer_notes or debate_result.get("rationale", ""),
    })
    return {"status": "rejected", "notify": out}


# --------------------------------------------------------------------------- #
# HITL -- real Action Center task, created directly (bypasses Case Mgmt/RCS)
# --------------------------------------------------------------------------- #
def create_hitl_task(claim: dict, debate_result: dict) -> dict:
    cls = debate_result["evidence"]["classifier"]
    vendor = claim.get("vendor") or "Unknown vendor"
    case_id = claim.get("case_id") or "unknown"
    body = {
        "AppId": HITL_APP_ID,
        "Title": f"Claim review — {vendor} ({case_id})",
        "Data": {
            "expenseType": str(cls.get("expense_type") or "others"),
            "riskScore": str(cls.get("risk_score") or ""),
            "classificationConfidence": str(cls.get("classification_confidence") or ""),
        },
    }
    resp = _call("POST", "/orchestrator_/tasks/AppTasks/CreateAppTask", ORCH_FOLDER_ID, body)
    return {"task_id": resp["id"], "task_key": resp["key"]}


def get_hitl_task(task_id: int) -> dict:
    """Read back a created App Task -- action is None until a reviewer decides
    (then "Approve" or "Reject", per classification-approval-app/action-schema.json)."""
    return _call(
        "GET",
        f"/orchestrator_/tasks/AppTasks/GetAppTaskById?taskId={task_id}",
        ORCH_FOLDER_ID,
    )


def resolve_hitl_outcome(claim: dict, debate_result: dict, task: dict) -> dict:
    """Called once a HITL task's `action` is no longer null. Dispatches to the
    same payout/reject-notify path a debate-driven AUTO_APPROVE/REJECT would."""
    action = (task.get("action") or "").strip().lower()
    reviewer_notes = ((task.get("data") or {}) or {}).get("reviewerNotes", "")
    if action == "approve":
        return fire_payout_and_notify(claim, debate_result)
    return fire_rejection_notify(claim, debate_result, reviewer_notes=reviewer_notes)


# --------------------------------------------------------------------------- #
# Admin-panel-driven decisions -- lets an authenticated reviewer approve/reject
# a pending claim from /admin directly (instead of Action Center's own UI).
# --------------------------------------------------------------------------- #
def get_current_user() -> str:
    """Username of the Orchestrator identity behind the current token --
    a freshly-created AppTask has no assignee, and CompleteAppTask rejects an
    action from an unassigned user (errorCode 2402), so this identity is who
    we assign the task to right before completing it."""
    resp = _call(
        "GET",
        "/orchestrator_/odata/Users/UiPath.Server.Configuration.OData.GetCurrentUser",
        ORCH_FOLDER_ID,
    )
    return resp["UserName"]


def assign_task(task_id: int, username: str) -> None:
    _call(
        "POST",
        "/orchestrator_/odata/Tasks/UiPath.Server.Configuration.OData.AssignTasks",
        ORCH_FOLDER_ID,
        {"taskAssignments": [{"TaskId": task_id, "UserNameOrEmail": username}]},
    )


def complete_app_task(task_id: int, action: str, data: dict | None = None) -> None:
    _call(
        "POST",
        "/orchestrator_/tasks/AppTasks/CompleteAppTask",
        ORCH_FOLDER_ID,
        {"taskId": task_id, "appId": HITL_APP_ID, "action": action, "data": data or {}},
    )


def decide_hitl_task(task_id: int, action: str, reviewer_notes: str = "") -> dict:
    """Assign + complete a pending HITL task as the authenticated admin,
    exactly as a human reviewer would in Action Center, then read it back.
    `action` must be "Approve" or "Reject"."""
    user = get_current_user()
    assign_task(task_id, user)
    complete_app_task(task_id, action, {"reviewerNotes": reviewer_notes})
    return get_hitl_task(task_id)


# --------------------------------------------------------------------------- #
# Dispatcher -- called once per debate result
# --------------------------------------------------------------------------- #
def act_on_verdict(claim: dict, debate_result: dict) -> dict:
    """Act on debate_result["path"] for real. Never raises -- callers treat a
    failure as best-effort and log it, the same as every other background
    action in this app; a failed action here should not crash the debate."""
    path = debate_result.get("path")
    try:
        if path in ("auto_approve", "proceed"):
            return fire_payout_and_notify(claim, debate_result)
        if path == "reject":
            return fire_rejection_notify(claim, debate_result)
        if path == "hitl_review":
            task = create_hitl_task(claim, debate_result)
            return {"status": "pending_review", **task}
        return {"status": "unknown_path", "path": path}
    except Exception as exc:  # best-effort, matches the rest of this codebase
        return {"status": "error", "error": str(exc)}
