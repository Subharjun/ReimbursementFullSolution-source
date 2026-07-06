# PolicyRuleCheckWorkflow — Stage 4 Policy Rule Bot (Subharjun)

AgentHack Track 1 · **Stage 4: Policy Check** (deterministic rule engine).

A UiPath **API Workflow** that takes the classified case and decides the routing outcome
against the policy DB: spend limit, date window, pre-approval, receipt requirement, plus the
risk/duplicate/business-purpose flags from Stage 3.

> Overlaps Mir's Stage-4 agent by design — confirm ownership with the team. This is a clean,
> standalone, fully-deterministic implementation Mir can use directly or as a reference.

## Inputs
`case_id, expense_type, amount, currency, date, document_attached, business_purpose_valid,
risk_score, duplicate_detected` and optional `policy_json` (stringified `mock_policy.json` to
override the embedded defaults — keeps a single source of truth).

## Output (the Stage-4 `policy` block of the case schema)
```json
{
  "case_id": "...",
  "within_spend_limit": true,
  "within_date_window": true,
  "preapproval_ok": true,
  "routing_decision": "auto_approve | manager_review | reject",
  "policy_violations": ["over_spend_limit", "missing_receipt", "..."],
  "audit_entry": { "stage": "policy_check", "stage_number": 4, "actor": "PolicyRuleCheckWorkflow", "actor_type": "api_workflow", "...": "..." }
}
```

## Routing rules
- **reject** — over spend limit, or duplicate detected.
- **auto_approve** — risk Low AND within limit AND amount ≤ category auto-approve threshold AND
  business purpose valid AND (receipt present or not required) AND within date window AND no
  pre-approval needed.
- **manager_review** — everything else.

Validated across: auto_approve (food 500), reject (travel 60000 over-limit), reject (duplicate),
manager_review + missing_receipt (medical no doc), manager_review (travel 8000 High).

## Run
```bash
uip api-workflow run ./PolicyRuleCheckWorkflow.json --no-auth \
  --input-arguments '{"case_id":"P1","expense_type":"food","amount":500,"currency":"INR","date":"2026-06-04","document_attached":true,"business_purpose_valid":true,"risk_score":"Low","duplicate_detected":false}' --output json
```

Keep the embedded category limits in sync with `../data/mock_policy.json`, or pass `policy_json`.
