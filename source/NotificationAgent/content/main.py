"""
AgentHack Track 1 - Stage 6 (notify): Reimbursement Notification Agent.

Owner: Subharjun.

A UiPath **coded LangGraph agent** that takes the **Stage 5a payout result**
(the Stripe PaymentIntent output) and emails the employee a polished, branded
HTML receipt via the tenant's Gmail Integration Service connection.

Graph:  START -> write_note (LLM) -> send (deterministic compose + Gmail) -> END

- write_note : uses the LLM (UiPath LLM Gateway, gpt-4o) to write a short, warm,
               human 1-2 sentence note addressed to the employee about their
               reimbursement status. The LLM writes PROSE ONLY -- it is never
               given authority over any financial fact (amount, IDs, status are
               injected deterministically in `send`). Falls back to a templated
               note when the LLM/Agent Units are unavailable, so the agent still
               runs locally without auth.
- send       : pure-Python -- resolves the sender identity from Orchestrator
               Assets, composes the HTML receipt (with the note woven in), and
               sends it through the Gmail `SendEmail` curated activity. Terminal
               node -> emits the full notification output.

Why the Gmail "Send Email" object (not the raw "Message"):
  - "SendEmail" is the curated activity that exposes a real Subject + HTML Body
    + CC/BCC + Reply-To + Importance, so the email actually looks good.
  - It has NO `From` field: Gmail always sends as the connected account, and an
    arbitrary `From` header would be rewritten by Gmail anyway. So "sender
    customizable from Orchestrator" is honored the way Gmail actually allows:
      * the brand/sender NAME shown in the email + the REPLY-TO address are read
        from Orchestrator Assets (overridable per-run via inputs), and
      * the transport identity is the Gmail connection itself, which is an
        Orchestrator-managed resource you can swap without code changes.

Orchestrator Assets (Text) read at run time (names overridable via input):
  - ReimbursementSenderName       -> display name in the email header/footer
  - ReimbursementReplyTo          -> Reply-To address (where replies land)
  - ReimbursementGmailConnectionId-> (optional) Integration Service connection id
                                     to send through; lets ops swap the Gmail
                                     account without redeploying. Falls back to
                                     DEFAULT_GMAIL_CONNECTION_ID below.

Why we resolve the connection by its real id (not a binding key):
  In a packaged SOLUTION deployment, the coded-agent connection *binding* is NOT
  surfaced as a configurable solution resource, so Orchestrator injects no
  override and `sdk.connections.retrieve("gmail-notify")` hits the API with the
  literal key -> 400 CNS1026 "The value 'gmail-notify' is not valid." Retrieving
  by the real connection id (Asset-overridable) makes the agent self-contained
  and deployment-portable.
"""

import os
import re
from datetime import datetime, timezone
from typing import Optional

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from uipath.eval.mocks import mockable
from uipath.platform.connections import ActivityMetadata, ActivityParameterLocationInfo

from savings import compute_savings, summary_line

# The real Gmail Integration Service connection id (tenant: hackathon26_332).
# This is the i.am.mir.jasim@gmail.com connection that lives IN the deployed
# solution folder (Shared/ReimbursementFullSolution). The serverless robot runs
# in that same folder, so it has Connections.View on it -> no CNS1045 403.
# (The old default 1481d829-... was in subharjun's PERSONAL WORKSPACE, which the
#  serverless robot cannot read -> the cloud Notify job kept 403'ing.)
# Overridable at runtime via the Orchestrator Text Asset 'ReimbursementGmailConnectionId'
# or the env var REIMBURSEMENT_GMAIL_CONNECTION_ID.
DEFAULT_GMAIL_CONNECTION_ID = "9291e875-b63f-4d6b-aaf0-84b81f41aa14"
GMAIL_CONNECTION_ID_ASSET = "ReimbursementGmailConnectionId"

# Gmail "Send Email" / Create (POST /SendEmail) - curated activity.
# Confirmed via `uip is resources describe uipath-google-gmail SendEmail`:
# request fields = To, CC, BCC, Subject, Body, ReplyTo, Importance; query = SaveAsDraft.
GMAIL_SEND = ActivityMetadata(
    object_path="/SendEmail",
    method_name="POST",
    content_type="multipart/form-data",
    parameter_location_info=ActivityParameterLocationInfo(
        query_params=["SaveAsDraft"],
        body_fields=["To", "CC", "BCC", "Subject", "Body", "ReplyTo", "Importance"],
    ),
    json_body_section="body",
)

# Default Orchestrator Asset names (overridable via input).
DEFAULT_SENDER_NAME_ASSET = "ReimbursementSenderName"
DEFAULT_REPLY_TO_ASSET = "ReimbursementReplyTo"

# Fallbacks used only when neither an input nor an Asset provides a value.
FALLBACK_SENDER_NAME = "Reimbursement Automation"

# Model used to write the personal note (prose only). Routed via UiPath LLM Gateway.
NOTE_MODEL = "gpt-4o-2024-11-20"

_CURRENCY_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "INR": "₹"}
_SUCCESS_STATES = {"succeeded", "success", "paid", "completed", "payout_initiated"}
_FAILED_STATES = {"failed", "canceled", "cancelled", "requires_payment_method", "declined"}


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class GraphInput(BaseModel):
    """Exactly what the agent needs: the Stage 5a Stripe payout output, plus the
    recipient and the sender identity I provide. Nothing else."""

    # --- recipient: passed separately; NOT part of the Stripe output ---
    employee_email: str = Field(description="Who to email (the claimant)")
    receipt_email: str = Field(default="", description="Fallback recipient if employee_email is blank (e.g. echoed by upstream)")
    employee_name: str = Field(default="", description="Claimant name for the greeting (optional)")

    # --- sender identity I give (Gmail always transmits as the connected
    #     account, so this drives the DISPLAY name + the Reply-To address the
    #     recipient sees and replies to) ---
    sender_name: str = Field(default="", description="Display/brand name shown as the sender")
    sender_email: str = Field(default="", description="Reply-To address replies should go to")

    # --- Stage 5a Stripe payout output (drop-in, exactly as produced) ---
    case_id: str = Field(default="", description="Maestro case ID")
    erp_system: str = Field(default="Stripe", description="Payment system label")
    payout_id: str = Field(default="", description="PaymentIntent / payout id")
    contact_id: str = Field(default="", description="Stripe customer id")
    fund_account_id: str = Field(default="", description="Payment method id")
    payout_status: str = Field(default="", description="e.g. succeeded")
    payment_status: str = Field(default="", description="e.g. payout_initiated")
    amount: float = Field(default=0.0, description="Amount actually charged")
    currency: str = Field(default="USD", description="ISO currency code")
    reference_id: str = Field(default="", description="Audit reference")
    submitted_at: str = Field(default="", description="ISO timestamp")

    # --- ROI / impact signals (optional; from Stage 3 classify + policy) ---
    duplicate_detected: bool = Field(default=False, description="Stage 3 flagged this as a duplicate (a real double-payment was averted)")
    discount_eligible: bool = Field(default=False, description="Claim qualified for an early-payment discount unlocked by fast approval")

    # --- test toggle ---
    save_as_draft: bool = Field(default=False, description="Draft instead of sending (for tests)")


class GraphOutput(BaseModel):
    sent: bool
    to: str = ""
    subject: str = ""
    message_id: str = ""
    sender_name: str = ""
    reply_to: str = ""
    personal_note: str = ""
    details: str = ""
    # quantified impact of automating this claim (for auditing / dashboards)
    impact_summary: str = ""
    savings: dict = Field(default_factory=dict)


class GraphState(BaseModel):
    # mirror of the input fields
    employee_email: str
    receipt_email: str = ""
    employee_name: str = ""
    sender_name: str = ""
    sender_email: str = ""
    case_id: str = ""
    erp_system: str = "Stripe"
    payout_id: str = ""
    contact_id: str = ""
    fund_account_id: str = ""
    payout_status: str = ""
    payment_status: str = ""
    amount: float = 0.0
    currency: str = "USD"
    reference_id: str = ""
    submitted_at: str = ""
    duplicate_detected: bool = False
    discount_eligible: bool = False
    save_as_draft: bool = False
    # produced by write_note
    personal_note: str = ""


# --------------------------------------------------------------------------- #
# Helpers (deterministic — financial facts only ever come from here)
# --------------------------------------------------------------------------- #
def _fmt_amount(amount: float, currency: str) -> str:
    cur = (currency or "USD").upper()
    sym = _CURRENCY_SYMBOLS.get(cur, "")
    try:
        n = float(amount or 0)
    except (TypeError, ValueError):
        n = 0.0
    body = f"{n:,.2f}"
    return f"{sym}{body} {cur}" if sym else f"{body} {cur}"


def _fmt_date(raw: str) -> str:
    if not raw:
        return datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    s = raw.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).strftime("%d %b %Y, %H:%M UTC")
    except ValueError:
        return raw


def _greeting_name(employee_name: str, employee_email: str) -> str:
    if employee_name:
        return employee_name.split()[0] if " " in employee_name else employee_name
    local = (employee_email or "").split("@")[0]
    local = local.replace(".", " ").replace("_", " ").strip()
    return local.split()[0].title() if local else "there"


_GREETING_RE = re.compile(
    r"^\s*(hi|hello|hey|dear)\b[ \t]*[^,\n—–-]*[,—–-]+[ \t]*",
    re.IGNORECASE,
)


def _strip_greeting(note: str) -> str:
    """Remove a leading salutation (e.g. 'Hi Mir,' / 'Hello Mir —') from the note.

    The email already prints its own 'Hi {name},' greeting line, and both the LLM
    and the deterministic fallback tend to open the note with the same salutation,
    which made the greeting appear twice. Strip a single leading greeting so the
    note reads as a continuation. Falls back to the original if stripping empties it.
    """
    stripped = _GREETING_RE.sub("", (note or "").strip(), count=1).strip()
    if not stripped:
        return (note or "").strip()
    return stripped[0].upper() + stripped[1:]


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _clean_email(raw: str) -> str:
    """Pull a single clean email address out of a possibly messy recipient value.

    Handles 'Name <a@b.com>' display forms, stray whitespace/newlines, angle
    brackets and surrounding quotes. Returns '' if no valid address is found
    (Gmail 400s 'Invalid To header' on anything that isn't a bare address).
    """
    s = (raw or "").strip().strip("<>").strip().strip('"').strip("'")
    if not s:
        return ""
    # 'Display Name <user@host>' -> user@host
    if "<" in s and ">" in s:
        inner = s[s.find("<") + 1 : s.find(">")].strip()
        if inner:
            s = inner
    m = _EMAIL_RE.search(s)
    return m.group(0) if m else ""


def _resolve_asset(sdk, name: str) -> str:
    """Best-effort Text-asset read; returns '' if missing/unavailable.

    In the deployed runtime the folder context is ambient. For local runs we
    fall back to the UIPATH_FOLDER_KEY env so folder-scoped assets resolve.
    """
    if not name:
        return ""
    import os

    folder_key = os.environ.get("UIPATH_FOLDER_KEY") or None
    for kwargs in ({"name": name, "folder_key": folder_key}, {"name": name}):
        try:
            asset = sdk.assets.retrieve(**kwargs)
        except Exception:
            continue
        value = (getattr(asset, "string_value", None) or getattr(asset, "value", None) or "").strip()
        if value:
            return value
    return ""


def _theme(payout_status: str, payment_status: str) -> tuple[str, str, str]:
    """Return (accent_hex, badge_label, status_word) based on payout state."""
    pay = (payout_status or "").lower()
    pstat = (payment_status or "").lower()
    if pay in _FAILED_STATES:
        return "#dc2626", "PAYMENT FAILED", "could not be completed"
    if pay in _SUCCESS_STATES or pstat in _SUCCESS_STATES:
        return "#16a34a", "PAID", "has been paid"
    return "#d97706", "PROCESSING", "is being processed"


def _esc(s: str) -> str:
    return (
        str(s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _row(label: str, value: str) -> str:
    if not value:
        return ""
    return (
        '<tr>'
        '<td style="padding:10px 0;border-bottom:1px solid #eceef1;color:#6b7280;'
        'font-size:13px;">' + _esc(label) + '</td>'
        '<td style="padding:10px 0;border-bottom:1px solid #eceef1;color:#111827;'
        'font-size:13px;font-weight:600;text-align:right;">' + _esc(value) + '</td>'
        '</tr>'
    )


def _fmt_usd(n: float) -> str:
    return f"${float(n or 0):,.2f}"


def _fmt_cur(n: float, currency: str) -> str:
    cur = (currency or "USD").upper()
    sym = _CURRENCY_SYMBOLS.get(cur, "")
    body = f"{float(n or 0):,.2f}"
    return f"{sym}{body} {cur}" if sym else f"{body} {cur}"


def _impact_item(icon: str, text: str) -> str:
    return (
        '<tr><td style="padding:6px 0;color:#0f766e;font-size:13px;line-height:1.5;">'
        f'<span style="display:inline-block;width:20px;">{icon}</span>{text}</td></tr>'
    )


def _impact_block(savings: dict) -> str:
    """A light 'Impact' card summarising the quantified savings for this claim.

    Only rendered when there is something meaningful to show. All figures come
    from the deterministic savings model (savings.py), never the LLM.
    """
    if not savings:
        return ""
    cur = savings.get("currency", "USD")
    items = [
        _impact_item(
            "⏱️",
            f"Processed in seconds — <b>~{savings['minutes_saved']:.0f} min</b> of manual effort saved",
        ),
        _impact_item(
            "💵",
            f"<b>~{_fmt_usd(savings['operational_saved_usd'])}</b> lower processing + digital-payout cost "
            "vs. a manual, paper-check process",
        ),
    ]
    if savings.get("duplicate_caught"):
        items.append(
            _impact_item(
                "🛡️",
                f"Duplicate detected — <b>{_fmt_cur(savings['duplicate_loss_prevented'], cur)}</b> "
                "double-payment prevented",
            )
        )
    else:
        items.append(_impact_item("🛡️", "Duplicate check passed — verified no double payment"))
    if savings.get("discount_captured", 0) > 0:
        items.append(
            _impact_item(
                "🏷️",
                f"Early-payment discount captured — <b>{_fmt_cur(savings['discount_captured'], cur)}</b>",
            )
        )
    return (
        '<tr><td style="padding:8px 32px 0 32px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background:#f0fdfa;border:1px solid #ccfbf1;border-radius:12px;">'
        '<tr><td style="padding:18px 20px 8px 20px;">'
        '<div style="color:#0f766e;font-size:12px;font-weight:700;text-transform:uppercase;'
        'letter-spacing:1px;margin-bottom:4px;">Automation impact</div>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        + "".join(items)
        + "</table></td></tr></table></td></tr>"
    )


def _fallback_note(name: str, status_word: str, badge: str) -> str:
    """Deterministic note used when the LLM is unavailable."""
    if badge == "PAID":
        return (
            f"Hi {name} — great news, your reimbursement {status_word}. "
            "Thanks for your patience while we processed it."
        )
    if badge == "PAYMENT FAILED":
        return (
            f"Hi {name} — unfortunately your reimbursement {status_word}. "
            "Our team is looking into it and will follow up shortly."
        )
    return (
        f"Hi {name} — a quick update: your reimbursement {status_word}. "
        "We'll let you know as soon as it's complete."
    )


def _compose(state: GraphState, sender_name: str, note: str) -> tuple[str, str, dict]:
    accent, badge, status_word = _theme(state.payout_status, state.payment_status)
    name = _greeting_name(state.employee_name, state.employee_email)
    note = _strip_greeting(note)  # avoid a second "Hi {name}," after the greeting line
    amount = _fmt_amount(state.amount, state.currency)
    when = _fmt_date(state.submitted_at)
    case = state.case_id or "N/A"

    # Quantified impact — only shown on a successful/processed payout, where a
    # real transaction actually happened (a failed payment saved nothing).
    savings = (
        compute_savings(
            amount=state.amount,
            currency=state.currency,
            duplicate_detected=state.duplicate_detected,
            payout_method=state.erp_system or "digital",
            discount_eligible=state.discount_eligible,
        )
        if badge != "PAYMENT FAILED"
        else {}
    )
    impact_html = _impact_block(savings)

    subject = (
        f"Your reimbursement {case} {status_word} — {amount}"
        if badge == "PAID"
        else f"Reimbursement {case}: payment {status_word}"
    )

    details = "".join(
        [
            _row("Case ID", state.case_id),
            _row("Payment method", state.erp_system or "Stripe"),
            _row("Payment reference", state.payout_id),
            _row("Status", (state.payout_status or state.payment_status or "").replace("_", " ").title()),
            _row("Processed on", when),
        ]
    )

    body = f"""\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f5f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7;padding:24px 12px;">
<tr><td align="center">
  <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:14px;overflow:hidden;box-shadow:0 1px 4px rgba(16,24,40,0.08);">
    <!-- header -->
    <tr><td style="background:{accent};padding:28px 32px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td style="color:#ffffff;font-size:13px;letter-spacing:.5px;text-transform:uppercase;opacity:.9;">{_esc(sender_name)}</td>
          <td align="right"><span style="display:inline-block;background:rgba(255,255,255,.18);color:#ffffff;font-size:11px;font-weight:700;letter-spacing:1px;padding:6px 12px;border-radius:999px;">{badge}</span></td>
        </tr>
      </table>
      <div style="color:#ffffff;font-size:24px;font-weight:700;margin-top:18px;">Reimbursement {status_word}</div>
    </td></tr>
    <!-- amount hero -->
    <tr><td style="padding:32px 32px 8px 32px;">
      <p style="margin:0 0 18px 0;color:#374151;font-size:15px;line-height:1.6;">Hi {_esc(name)},</p>
      <p style="margin:0 0 24px 0;color:#374151;font-size:15px;line-height:1.6;">{_esc(note)}</p>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f9fafb;border:1px solid #eceef1;border-radius:12px;">
        <tr><td align="center" style="padding:24px;">
          <div style="color:#6b7280;font-size:12px;text-transform:uppercase;letter-spacing:1px;">Amount {('paid' if badge=='PAID' else 'requested')}</div>
          <div style="color:{accent};font-size:34px;font-weight:800;margin-top:6px;">{_esc(amount)}</div>
        </td></tr>
      </table>
    </td></tr>
    <!-- details -->
    <tr><td style="padding:8px 32px 8px 32px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
        {details}
      </table>
    </td></tr>
    <!-- impact -->
    {impact_html}
    <!-- note -->
    <tr><td style="padding:20px 32px 0 32px;">
      <p style="margin:0;color:#6b7280;font-size:13px;line-height:1.6;">
        If anything looks off, just reply to this email and our team will help you out.
      </p>
    </td></tr>
    <!-- footer -->
    <tr><td style="padding:28px 32px;">
      <hr style="border:none;border-top:1px solid #eceef1;margin:0 0 16px 0;">
      <p style="margin:0;color:#9ca3af;font-size:12px;line-height:1.6;">
        This is an automated message from {_esc(sender_name)} (UiPath reimbursement pipeline).<br>
        Reference: {_esc(state.reference_id or state.case_id)}
      </p>
    </td></tr>
  </table>
</td></tr>
</table>
</body>
</html>"""
    return subject, body, savings


# --------------------------------------------------------------------------- #
# Node 1 - write_note (LLM writes PROSE ONLY; deterministic fallback)
# --------------------------------------------------------------------------- #
async def write_note(state: GraphState) -> dict:
    accent, badge, status_word = _theme(state.payout_status, state.payment_status)
    name = _greeting_name(state.employee_name, state.employee_email)
    fallback = _fallback_note(name, status_word, badge)

    system_prompt = (
        "You are a friendly enterprise reimbursement assistant. Write ONE short, warm, "
        "professional message (1-2 sentences, max ~30 words) to an employee about the "
        "status of their expense reimbursement.\n"
        "STRICT RULES:\n"
        "- Do NOT invent or state any amount, currency, payment id, date, or account detail "
        "(those are shown separately in the email).\n"
        "- Do NOT use placeholders or brackets.\n"
        "- Address the employee by first name, naturally.\n"
        "- Match the tone to the status: celebratory if paid, reassuring if processing, "
        "apologetic + supportive if failed.\n"
        "- Output only the message text, nothing else."
    )
    user_prompt = (
        f"Employee first name: {name}\n"
        f"Reimbursement status: {status_word} (badge: {badge})\n"
        "Write the message now."
    )

    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from uipath_langchain.chat.models import UiPathChat

        llm = UiPathChat(model=NOTE_MODEL, temperature=0.6, max_tokens=120)
        resp = await llm.ainvoke([SystemMessage(system_prompt), HumanMessage(user_prompt)])
        text = (getattr(resp, "content", "") or "").strip()
        # Guard against an empty / overlong reply.
        if not text:
            text = fallback
        elif len(text) > 320:
            text = text[:317].rstrip() + "..."
        return {"personal_note": text}
    except Exception:
        # No auth / no Agent Units / gateway error -> deterministic templated note.
        return {"personal_note": fallback}


# --------------------------------------------------------------------------- #
# Gmail send — isolated + @mockable so evaluations run without the live
# Integration Service connection. In production the real function runs.
# --------------------------------------------------------------------------- #
def _resolve_connection_id(sdk) -> str:
    """Real Gmail connection id: env -> Orchestrator Asset -> baked-in default."""
    return (
        os.environ.get("REIMBURSEMENT_GMAIL_CONNECTION_ID")
        or _resolve_asset(sdk, GMAIL_CONNECTION_ID_ASSET)
        or DEFAULT_GMAIL_CONNECTION_ID
    )


@mockable()
def gmail_send(connection_id: str, activity_input: dict) -> dict:
    from uipath.platform import UiPath  # lazy: never instantiate at module level

    sdk = UiPath()
    # Retrieve by the real connection id (works in standalone + solution deploys).
    connection = sdk.connections.retrieve(connection_id)
    resp = sdk.connections.invoke_activity(
        activity_metadata=GMAIL_SEND,
        connection_id=connection.id,
        activity_input=activity_input,
    )
    return resp if isinstance(resp, dict) else {}


# --------------------------------------------------------------------------- #
# Node 2 - send (deterministic; terminal node emits full output)
# --------------------------------------------------------------------------- #
async def send(state: GraphState) -> GraphOutput:
    from uipath.platform import UiPath  # lazy: never instantiate at module level

    sdk = UiPath()

    # Sender identity I provide: input -> Orchestrator Asset -> fallback.
    sender_name = state.sender_name or _resolve_asset(sdk, DEFAULT_SENDER_NAME_ASSET) or FALLBACK_SENDER_NAME
    reply_to = state.sender_email or _resolve_asset(sdk, DEFAULT_REPLY_TO_ASSET)

    note = state.personal_note or _fallback_note(
        _greeting_name(state.employee_name, state.employee_email),
        _theme(state.payout_status, state.payment_status)[2],
        _theme(state.payout_status, state.payment_status)[1],
    )
    subject, body, savings = _compose(state, sender_name, note)

    # Recipient guard: Gmail 400s with "Recipient address required" on a blank To
    # and "Invalid To header" on a malformed one (e.g. a name, or 'Name <addr>').
    # Clean+validate to a bare address; fail loud with the offending value otherwise.
    raw_to = state.employee_email or state.receipt_email or ""
    to = _clean_email(raw_to)
    if not to:
        raise ValueError(
            "No valid recipient email. employee_email/receipt_email did not contain "
            f"a parseable address (got {raw_to.strip()!r}). The recipient is passed "
            "separately from the Stripe payout output — ensure the upstream chain "
            "carries the claimant's email address (not their name) into employee_email."
        )

    activity_input = {
        "To": to,
        "Subject": subject,
        "Body": body,
        "Importance": "normal",
        "SaveAsDraft": state.save_as_draft,
    }
    if reply_to:
        activity_input["ReplyTo"] = reply_to

    resp = gmail_send(_resolve_connection_id(sdk), activity_input)

    message_id = resp.get("id", "") if isinstance(resp, dict) else ""
    verb = "Drafted" if state.save_as_draft else "Sent"
    impact_summary = summary_line(savings) if savings else ""
    return GraphOutput(
        sent=bool(message_id),
        to=to,
        subject=subject,
        message_id=message_id,
        sender_name=sender_name,
        reply_to=reply_to,
        personal_note=note,
        details=f"{verb} '{subject}' to {to}",
        impact_summary=impact_summary,
        savings=savings,
    )


# --------------------------------------------------------------------------- #
# Graph
# --------------------------------------------------------------------------- #
builder = StateGraph(GraphState, input_schema=GraphInput, output_schema=GraphOutput)
builder.add_node("write_note", write_note)
builder.add_node("send", send)
builder.add_edge(START, "write_note")
builder.add_edge("write_note", "send")
builder.add_edge("send", END)

graph = builder.compile()
