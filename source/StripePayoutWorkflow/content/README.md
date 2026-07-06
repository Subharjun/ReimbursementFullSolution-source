# StripePayoutWorkflow — Stage 5a (Subharjun)

AgentHack Track 1 · **Stage 5a: ERP Payment / Reimbursement Payout (Stripe)**

A UiPath **API Workflow** that disburses an approved reimbursement to the employee via the
**Stripe API**. This is the live replacement for the RazorpayX workflow — RazorpayX onboarding
wasn't available, whereas **Stripe test keys are free, instant, and self-serve**, so this
integration is fully runnable by anyone with a Stripe test account.

> **Unlike Razorpay (which only reached a `401` on placeholder creds), this workflow completes a
> REAL Stripe test-mode disbursement end-to-end** — it creates a customer and a confirmed
> PaymentIntent that returns `status: succeeded`. Credentials are still kept as workflow inputs
> (or Orchestrator Assets), never hardcoded.

## Call chain (all live, validated against `api.stripe.com`)
```
WorkflowStart
 → BuildAuth_1          (JS)   Bearer auth header + form-encoded request bodies
 → HttpRequest_Customer (HTTP) POST /v1/customers         (create employee/payee)
 → ExtractContact_1     (JS)   contact_id (Stripe customer id)
 → HttpRequest_Payout   (HTTP) POST /v1/payment_intents   (amount in cents, confirm=true, pm_card_visa)
 → BuildOutput_1        (JS)   assemble Stage-5a contract
 → Response_1
```
Validated: real test run returned a `succeeded` PaymentIntent
(`pi_…`) for a `cus_…` customer.

### Why no separate "fund account" step
RazorpayX needed Contact → Fund Account → Payout. Stripe collapses the payment-instrument step:
the confirmed PaymentIntent uses Stripe's standard test PaymentMethod `pm_card_visa`. The
customer is intentionally **not** attached to the PaymentIntent so the shared test token can be
reused across repeated runs without an "already attached to another customer" error. Swap in a
real customer PaymentMethod (or use **Stripe Connect Transfers** to a connected account) for a
true employee-bound disbursement.

## The credentials (the only thing to fill)
Get them free at **dashboard.stripe.com → Developers → API keys** (Test mode):

| Input | Where to get it | Example |
|---|---|---|
| `stripe_secret_key` | Developers → API keys → Secret key | `sk_test_…` |
| `stripe_publishable_key` | Developers → API keys → Publishable key | `pk_test_…` (accepted but not used server-side) |

Only `stripe_secret_key` is used server-side. `stripe_publishable_key` is accepted for
completeness/parity but is a client-side key.

**Production wiring:** bind `stripe_secret_key` to an Orchestrator Credential Asset
`STRIPE_SECRET_KEY`; the Maestro Case passes it in. To activate the whole stage, set that one
Asset value — nothing else changes.

## Other inputs (from the approved case)
`case_id, employee_email, employee_name, amount, currency (default usd), expense_type, reason`.

## Output (Stage-5a contract → written to the Maestro Case)
Identical schema to the old Razorpay workflow, so it's a drop-in downstream:
```json
{
  "case_id": "...", "erp_system": "Stripe",
  "contact_id": "cus_...", "fund_account_id": "pm_...",
  "payout_id": "pi_...", "payout_status": "succeeded|processing|requires_capture|...",
  "payment_status": "payout_initiated", "amount": 1800, "currency": "USD",
  "reference_id": "...", "submitted_at": "<ISO8601>"
}
```
`payment_status` is `payout_initiated` when the PaymentIntent status is
`succeeded` / `processing` / `requires_capture`, else `payout_failed`.

## Run it
```bash
# real test disbursement (returns a succeeded PaymentIntent with your test keys):
uip api-workflow run ./StripePayoutWorkflow.json --no-auth \
  --input-arguments '{"case_id":"T1","employee_email":"e@co.com","employee_name":"E","amount":1800,"currency":"usd","expense_type":"travel","reason":"cab","stripe_secret_key":"sk_test_...","stripe_publishable_key":"pk_test_..."}' --output json
```

Notes: amount is sent in the **smallest currency unit** (cents) — `amount × 100` — same convention
Razorpay used for paise. Default currency is `usd` (typical Stripe test account).

## Razorpay / SAP Concur alternatives
- `../RazorpayPayoutWorkflow/` — previous INR/RazorpayX variant (reached the live API; superseded by Stripe).
- `../SAPConcurPaymentWorkflow/` — enterprise-ERP variant (real, validated to reach live Concur),
  kept for reference but not live (Concur API access is partner-gated, no free sandbox).
