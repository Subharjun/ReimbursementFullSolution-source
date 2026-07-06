# NotificationAgent — Stage 6 Notifications (Subharjun)

AgentHack Track 1 · **Stage 6: Notify** — emails the employee (and manager) on decision and
payment events via the tenant's **Gmail Integration Service connection**.

> Real integration — validated end-to-end (a real email was sent; Gmail message id returned).
> No mock.

## What it does
- `event_type = "decision"` → emails employee + manager with the approve/reject/route outcome.
- `event_type = "payment"`  → emails employee with the SAP Concur payment result.

Gmail `Message/Create` is a **multipart** curated activity whose `body` part holds the JSON
fields (`to`, `data`, …) and `file` part holds attachments. It has no dedicated subject field,
so the subject is rendered as the first line of the body.

## Inputs (subset used per event)
`case_id, employee_email, manager_email, event_type, decision, expense_type, amount, currency,
risk_score, routing_decision, reason, payment_status, concur_report_id, payment_url`

## Output
```json
{ "sent": true, "recipients": ["..."], "message_ids": ["19e9..."], "subject": "...", "details": "..." }
```

## Connection wiring
- `bindings.json` → connection resource `gmail-notify` (deploy-time; binding key only, no secrets).
- `__uipath/uipath.json` → local-run `resourceOverwrites` mapping `connection.gmail-notify` to
  YOUR Gmail connection's UUID + folder key. **Not committed** (git-ignored). Copy the template:
  ```bash
  cp __uipath/uipath.json.example __uipath/uipath.json
  # then fill in YOUR connection IDs:
  uip is connections list --connector-key uipath-google-gmail --output json
  #   -> use "Id" as connectionId and "FolderKey" as folderKey
  ```
- The `ActivityMetadata` (`GMAIL_SEND`) was sourced from `uip is resources describe` + the raw
  multipart schema — do not hand-edit it; re-derive if the connector version changes.

> Prerequisite: a Gmail Integration Service connection on your tenant
> (`uip is connections create uipath-google-gmail`). The agent has no hardcoded account.

## Run locally (sends a REAL email)
```bash
source .venv/bin/activate
export UIPATH_FOLDER_KEY=<YOUR_GMAIL_CONNECTION_FOLDER_KEY>   # from `uip is connections list`
uip codedagent run main '{"case_id":"REIMB-TEST","employee_email":"you@example.com","event_type":"decision","decision":"approved","expense_type":"travel","amount":7000,"currency":"INR","risk_score":"High","routing_decision":"manager_review","reason":"client meeting"}'
```

## Deploy
```bash
uip codedagent deploy --tenant
```

## ⚠️ Toolchain note (multi-project repos)
`uip codedagent setup --force` repoints the global CLI's Python at the *current* project's venv.
After working in another coded agent, re-run `source .venv/bin/activate && uip codedagent setup --force`
in the project you want to run, or its entrypoint won't resolve.

## Slack (optional, not wired)
No Slack connection exists on the tenant. To add Slack notifications, create a Slack connection
(`uip is connections create uipath-slack`) and add a second `ActivityMetadata` for
`send_message_to_channel_v2`, or POST to a Slack Incoming Webhook via the HTTP activity.
