# Reimbursement Intake v2

One Render service, three surfaces:

| Route | Auth | What |
|---|---|---|
| `/` | none | the intake form (React SPA) |
| `/dashboard` | none | live multi-agent consensus debate console |
| `/admin` | Basic | live ops dashboard — job/release health, audit export |

This is the 2.0 counterpart of `reimbursement-intake`, wired entirely to
**our own** `Shared/ReimbursementFullSolution` folder instead of Mir's
original folder, merged with the live-ops/consensus-debate dashboard
(previously a separate service, `reimbursement-admin-dashboard`) so both
live on one Render site as requested.

## What happens on submit (`/`)

1. Uploads the receipt to the `Receipt` bucket (id `232381`) in
   `Shared/ReimbursementFullSolution`.
2. Triggers **MirCaseClone** (our own cloned Maestro Case) via Orchestrator
   `StartJobs` — **this is the real decision path**: `NotificationAgent`
   and `RejectionNotificationAgent` are stages *inside* MirCaseClone itself
   (Payout → Notify, Rejection → RejectNotify), fired once the case reaches
   that stage, not called directly by this app. See `HANDOFF.md` item 19 for
   the one known gap (Human-Review/HITL's Resource-Catalog issue) —
   everything up through Policy, and Payout/Reject when a case resolves
   without needing HITL, executes for real.
3. Fires **SubmissionConfirmationAgent** twice in the background — submitter
   and manager — real Gmail sends ("request received, verdict in under 5
   minutes"), reaching any real address.
4. Also fires a Resend-based finance/manager notification + submitter
   fallback, mirroring the main project's belt-and-suspenders pattern
   (subject to Resend's free-tier "only reaches the account owner's inbox"
   restriction unless a sending domain is verified).
5. **Also** runs the live Consensus Engine debate (Classify/Fraud/Policy/
   Consensus, the actual deployed processes) in the background, purely for
   **observability** on `/dashboard` — it does *not* dispatch payout/
   notify/reject itself, so there's no double-firing against the same claim.
   MirCaseClone (step 2) is the only real decision path.

## Env vars

See `.env.example`. Required: `UIPATH_CLIENT_ID` + `UIPATH_CLIENT_SECRET`
(or `UIPATH_ACCESS_TOKEN`); `ADMIN_USER` + `ADMIN_PASSWORD` (for `/admin`).
Optional: `RESEND_API_KEY` (+ `RESEND_NOTIFY_TO`).

`STRIPE_SECRET_KEY`/`STRIPE_PUBLISHABLE_KEY` are **not** consumed by this
app — the Stripe payout happens inside MirCaseClone's own
`StripePayoutWorkflow` stage, bound once in Studio Web, not passed from
here. They're only listed in `.env.example`/`render.yaml` in case you later
extend `consensus/orchestrator.py`'s (currently unused here) debate-driven
payout path.

## Local dev

```bash
cp .env.example .env
npm install
pip install -r requirements.txt
# terminal 1
uvicorn api:app --reload --port 8002
# terminal 2
npm run dev
```

## Deploy to Render

1. Push to GitHub (done if you're reading this from the repo).
2. Render → **New → Web Service** → connect this repo (Docker/`render.yaml`
   auto-detected).
3. Set env vars in Render's dashboard: `UIPATH_CLIENT_ID`/`UIPATH_CLIENT_SECRET`,
   `ADMIN_USER`/`ADMIN_PASSWORD`, optionally `RESEND_API_KEY`.
4. Deploy. `/` is the public form, `/dashboard` is the public debate console,
   `/admin` needs the Basic-auth creds you set.

## `reimbursement-admin-dashboard` (the other repo)

That repo is now superseded by this merged service — its `/admin` and
`/console` are the same code, now living here instead. It hasn't been
deleted; if you don't plan to deploy it separately anymore you can archive
or remove it later, just make sure nothing on Render still points at it.
