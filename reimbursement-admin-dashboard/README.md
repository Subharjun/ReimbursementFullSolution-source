# Reimbursement Admin Dashboard

Standalone live-ops dashboard + multi-agent consensus debate console for the
`Shared/ReimbursementFullSolution` UiPath folder. Split out of
`reimbursement-intake` (the public intake form) into its own repo/service so
this feature can't destabilize the main submission flow again — it shipped
there once, then was fully reverted after breaking the main app. This repo
is the same, previously-validated code, running independently.

## What it connects

Every "open" (unprotected) component in `Shared/ReimbursementFullSolution`,
in one dashboard:

- **ReimbursementClassificationAgent + FraudIntegrityAgent +
  PolicyRuleCheckWorkflow + ConsensusArbitrationWorkflow** — `consensus/`
  runs a real 3-round multi-agent debate against the actual deployed
  processes for any claim (`POST /api/debate`, or automatically for any
  submission passed through this dashboard's own trigger).
- **MirCaseClone** (and every other release in the folder) — `/api/admin/overview`
  reads live Jobs/Releases health directly from Orchestrator for the whole
  folder.
- **The orchestrator's own verdict is the real decision path** (not Mir's
  Case): `AUTO_APPROVE`/`PROCEED` fires `StripePayoutWorkflow` +
  `NotificationAgent`, `REJECT` fires `RejectionNotificationAgent`,
  `HITL_REVIEW` creates a real Action Center task against
  `classification-approval-app` directly — see `consensus/orchestrator.py`
  for why this bypasses `MirCaseClone`'s Human-Review step, which has a
  persistent Resource-Catalog gap for that app in this folder.
- **SubmissionConfirmationAgent** — `/api/admin/test-confirmation` fires a
  real test job so it can be demoed/verified without a real form submission.

The 7 protected components (`NotificationAgent`, `PolicyRuleCheckWorkflow`,
`ReceiptExtractor_XP`, `ReimbursementClassificationAgent`,
`ReimbursementIntakeBot_XP`, `RejectionNotificationAgent`,
`StripePayoutWorkflow`) are **read-only** here — this dashboard only starts
jobs against them the same way the intake app already does, never modifies
their definitions/deployments.

## Routes

| Route | Auth | Purpose |
|---|---|---|
| `GET /` | none | live debate console (`live_console.html`) |
| `GET /console` | none | same as `/` |
| `GET /api/debates`, `/api/debates/latest` | none | debate feed for the console to poll |
| `POST /api/debate` | Basic | manually trigger a live debate for a demo claim |
| `GET /admin` | Basic | ops dashboard (`admin.html`) — job health, pending HITL, audit export |
| `GET /api/admin/overview` | Basic | live Jobs/Releases health for the whole folder |
| `GET /api/admin/pending-hitl` | Basic | claims currently waiting on a human reviewer |
| `POST /api/admin/hitl/{case_id}/decide` | Basic | approve/reject a pending claim from the dashboard |
| `GET /api/audit/{case_id}` | Basic | downloadable JSON audit record for one case |
| `POST /api/admin/test-confirmation` | Basic | fire a real test job against SubmissionConfirmationAgent |

Basic auth is `ADMIN_USER` / `ADMIN_PASSWORD` (env vars, no default — every
admin route fails closed with a 500 if either is unset).

## Local dev

```bash
cp .env.example .env   # fill in UIPATH_CLIENT_ID/SECRET + ADMIN_USER/PASSWORD
pip install -r requirements.txt
uvicorn api:app --reload --port 8001
```

## Deploy to Render

1. Push this repo to GitHub (already done if you're reading this from the repo).
2. On Render: **New → Web Service → connect this GitHub repo** (Docker
   runtime is auto-detected via `render.yaml`/`Dockerfile`).
3. Set env vars in the Render dashboard (not committed — see `.env.example`):
   `UIPATH_CLIENT_ID`, `UIPATH_CLIENT_SECRET` (or `UIPATH_ACCESS_TOKEN`),
   `ADMIN_USER`, `ADMIN_PASSWORD`.
4. Deploy. `/` is the public debate console; `/admin` needs Basic auth.

## Trigger a demo debate

```bash
curl -u "$ADMIN_USER:$ADMIN_PASSWORD" -X POST https://<your-render-url>/api/debate \
  -H 'Content-Type: application/json' \
  -d '{
    "vendor": "Test Vendor Co",
    "amount": 1500,
    "currency": "INR",
    "date": "2026-07-06",
    "expense_type": "others",
    "employee_email": "you@example.com",
    "employee_name": "Test User",
    "purpose": "Client dinner",
    "document_attached": true,
    "ocr_confidence": 0.95
  }'
```
