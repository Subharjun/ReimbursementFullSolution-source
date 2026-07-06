# RejectionNotificationAgent

AgentHack Track 1 — **classification-REJECT notifier**. A coded LangGraph agent
that fires only on the **Reject** outcome of the Classification approval Action App
in the Maestro Case, and emails the claimant a polished "your reimbursement was
**not approved**" message via the tenant Gmail Integration Service connection.

It is a sibling of `NotificationAgent` (the payout-success notifier) — same graph
shape, same Gmail connection (`9291e875-…`, in `Shared/ReimbursementFullSolution`),
same `_clean_email` recipient hardening — but the content is a rejection (red
"Not Approved" theme, reviewer notes surfaced verbatim, next-steps), not a receipt.

## Graph
`START → write_note (LLM, prose only + deterministic fallback) → send (deterministic HTML compose + Gmail SendEmail) → END`

The LLM only writes the warm 1–2 sentence note; every verdict fact (case id,
amount, reviewer notes, decision) is injected deterministically in `send`.

## Input (only `employee_email` is required)
| field | purpose |
|---|---|
| `employee_email` | recipient (claimant). `receipt_email` is a fallback. |
| `employee_name` | greeting name (else derived from the email local-part) |
| `sender_name` / `sender_email` | display name + Reply-To (else Orchestrator Assets `ReimbursementSenderName` / `ReimbursementReplyTo`) |
| `case_id`, `expense_type`, `vendor`, `amount`, `currency` | shown in the details block |
| `risk_score`, `classification_confidence` | context only — NOT shown to the claimant |
| `reviewer_notes` | the reviewer's reason for rejecting — shown verbatim (falls back to `reason`) |
| `save_as_draft` | draft instead of send (tests) |

Output mirrors `NotificationAgent`: `sent, to, subject, message_id, sender_name, reply_to, personal_note, details`.

## Run / validate locally
```bash
uv sync
# smoke eval (Gmail mocked) — needs base URL + token for the SDK to construct:
export UIPATH_URL=https://staging.uipath.com/hackathon26_332/DefaultTenant
export UIPATH_ACCESS_TOKEN=$(grep '^UIPATH_ACCESS_TOKEN=' ~/.uipath/.auth | cut -d= -f2-)
uv run uipath eval --no-report          # -> 2/2 = 1.0

# real draft send as your user:
export REIMBURSEMENT_GMAIL_CONNECTION_ID=9291e875-b63f-4d6b-aaf0-84b81f41aa14
uv run uipath run agent '{"employee_email":"you@example.com","case_id":"1085","expense_type":"travel","amount":228.92,"reviewer_notes":"Receipt does not match the claimed dates.","save_as_draft":true}'
```

## Wiring into the Maestro Case
On the **Reject** branch of the Classification gateway, add an **agent** task
pointing at this agent and map its inputs from the case variables
(`employee_email`, `case_id`, `expense_type`, `amount`, `reviewer_notes` = the
approval app's `reviewerNotes` output). Then it sends the rejection notice before
the case ends. Deploy alongside the existing solution (or as a standalone Agent
package) the same way `NotificationAgent` is deployed.
