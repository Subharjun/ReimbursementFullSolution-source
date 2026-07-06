# FraudIntegrityAgent

AgentHack Track 1 — reimbursement **fraud / integrity screening** agent (coded LangGraph).

Screens a claim for fraud risk **before payout**. This is the pipeline's answer to
"invoice processing is commoditised": a plain OCR/AP tool reads a receipt, but this
agent *reasons* about whether the claim can be trusted.

## Design — the code decides, the LLM only explains

```
START ─▶ detect (deterministic) ─▶ explain (LLM prose) ─▶ END
```

- **`detect`** — pure Python (`detectors.py`, no LLM, no I/O) turns the claim (+ the
  claimant's recent `claim_history`) into a fraud score, a risk band, and concrete
  flags with **transparent, env-tunable thresholds**. Reproducible and auditable —
  never a black box.
- **`explain`** — the LLM (UiPath LLM Gateway, gpt-4o) writes a short investigator
  narrative *of the flags the detector produced*. It never invents a flag, number, or
  verdict; if Agent Units are unavailable it falls back to a deterministic summary, so
  the agent runs locally with no auth.

## What it catches

| Flag | Meaning |
|---|---|
| `DUPLICATE` | Same vendor+amount resubmitted → double-payment risk |
| `SPLIT_CLAIM` | Several sub-threshold claims to one vendor that together clear the approval limit |
| `THRESHOLD_HUG` | A single amount parked just under the approval limit |
| `VELOCITY` | Implausible burst of claims from one person |
| `ROUND_AMOUNT` / `WEEKEND_DATE` | Weak contextual signals |

## Output (superset drop-in for the Maestro Case)

`fraud_score` (0–100), `integrity_risk` (Low/Medium/High), `recommendation`
(proceed/review/reject), `duplicate_detected`, `split_claim_detected`, `flags[]`,
`explanation`, `summary`, `assumptions`.

`duplicate_detected` is the exact signal the **NotificationAgent** ROI model credits as
"duplicate double-payment prevented" — so this agent makes that saving *earned*, not assumed.

## Tuning (env vars)

`FRAUD_APPROVAL_THRESHOLD`, `FRAUD_THRESHOLD_HUG_PCT`, `FRAUD_DUP_WINDOW_DAYS`,
`FRAUD_SPLIT_WINDOW_DAYS`, `FRAUD_VELOCITY_WINDOW_DAYS`, `FRAUD_VELOCITY_MAX`,
`FRAUD_HIGH_AT`, `FRAUD_MEDIUM_AT`. All defaults follow standard AP norms and are
returned in the output `assumptions` block.

## Run / eval locally

```bash
uv sync
uv run uipath run agent '{"vendor":"Hotel Grand","amount":228.92,"date":"2026-06-04","employee_email":"a@x.com","claim_history":[{"vendor":"Hotel Grand","amount":228.92,"date":"2026-06-01","employee_email":"a@x.com","case_id":"OLD-77"}]}'
uv run uipath eval
```

No Integration Service connection required (pure analysis) → no bindings.
