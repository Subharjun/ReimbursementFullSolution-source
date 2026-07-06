"""
AgentHack Track 1 - Submission confirmation notifier: SubmissionConfirmationAgent.

Owner: Subharjun.

A UiPath **coded LangGraph agent** that fires immediately after the intake form
is submitted -- BEFORE the rest of the pipeline (IDP/Classify/Fraud/Policy/
Payout) runs. It tells the submitter "your reimbursement request was received
and you'll hear a verdict in under 5 minutes" via the tenant's Gmail
Integration Service connection.

Built to replace the Render intake's Resend-based confirmation email, which
only ever delivered to the Resend account owner's own inbox (free tier, no
verified sending domain) -- so real submitters never actually received it.
Sending through the same real Gmail connection the other reimbursement agents
already use has no such restriction: it can send to any real address.

It is a sibling of `NotificationAgent` / `RejectionNotificationAgent` and
shares their design exactly -- same graph shape, same Gmail connection, same
recipient hardening -- but the email is a lightweight "we got it" receipt,
not a payout/rejection verdict, and it is meant to be triggered standalone
(directly from the intake backend) rather than as a Maestro Case stage.

Graph:  START -> write_note (LLM) -> send (deterministic compose + Gmail) -> END

- write_note : uses the LLM (UiPath LLM Gateway, gpt-4o) to write a short, warm,
               1-sentence acknowledgement. PROSE ONLY -- it never states the
               verdict-timing fact (that line is injected deterministically in
               `send`). Falls back to a templated note when the LLM/Agent Units
               are unavailable, so the agent still runs locally without auth.
- send       : pure-Python -- resolves the sender identity from Orchestrator
               Assets, composes the "Request received" HTML email, and sends
               it through the Gmail `SendEmail` curated activity. Terminal
               node -> emits the full output.
"""

import os
import re

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from uipath.eval.mocks import mockable
from uipath.platform.connections import ActivityMetadata, ActivityParameterLocationInfo

# The real Gmail Integration Service connection id (tenant: hackathon26_332).
# Same i.am.mir.jasim@gmail.com connection that lives IN the deployed solution
# folder (Shared/ReimbursementFullSolution) -> serverless robot has Connections.View
# on it, so no CNS1045 403. Overridable at runtime via the Orchestrator Text Asset
# 'ReimbursementGmailConnectionId' or env REIMBURSEMENT_GMAIL_CONNECTION_ID.
DEFAULT_GMAIL_CONNECTION_ID = "9291e875-b63f-4d6b-aaf0-84b81f41aa14"
GMAIL_CONNECTION_ID_ASSET = "ReimbursementGmailConnectionId"

# Gmail "Send Email" / Create (POST /SendEmail) - curated activity.
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

# Fallback used only when neither an input nor an Asset provides a value.
FALLBACK_SENDER_NAME = "Reimbursement Automation"

# Model used to write the acknowledgement note (prose only). Routed via UiPath LLM Gateway.
NOTE_MODEL = "gpt-4o-2024-11-20"

ACCENT = "#2563eb"
BADGE = "RECEIVED"

_CURRENCY_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "INR": "₹"}


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class GraphInput(BaseModel):
    """What the agent needs for a submission-received email: the claimant +
    sender identity, and enough claim context to make the receipt useful."""

    # --- recipient: passed separately ---
    employee_email: str = Field(description="Who to email (the claimant/submitter)")
    receipt_email: str = Field(default="", description="Fallback recipient if employee_email is blank")
    employee_name: str = Field(default="", description="Claimant name for the greeting (optional)")

    # --- sender identity I give (drives DISPLAY name + Reply-To the recipient sees) ---
    sender_name: str = Field(default="", description="Display/brand name shown as the sender")
    sender_email: str = Field(default="", description="Reply-To address replies should go to")

    # --- claim context (for the receipt / the LLM tone) ---
    case_id: str = Field(default="", description="Maestro case ID, if already known at intake time")
    expense_type: str = Field(default="", description="Expense type as entered on the form")
    vendor: str = Field(default="", description="Vendor / merchant on the claim")
    amount: float = Field(default=0.0, description="Amount requested")
    currency: str = Field(default="USD", description="ISO currency code")

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


class GraphState(BaseModel):
    # mirror of the input fields
    employee_email: str
    receipt_email: str = ""
    employee_name: str = ""
    sender_name: str = ""
    sender_email: str = ""
    case_id: str = ""
    expense_type: str = ""
    vendor: str = ""
    amount: float = 0.0
    currency: str = "USD"
    save_as_draft: bool = False
    # produced by write_note
    personal_note: str = ""


# --------------------------------------------------------------------------- #
# Helpers (deterministic — verdict-timing fact always comes from here)
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
    which would make the greeting appear twice. Strip a single leading greeting so
    the note reads as a continuation. Falls back to the original if stripping empties it.
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
    if "<" in s and ">" in s:
        inner = s[s.find("<") + 1 : s.find(">")].strip()
        if inner:
            s = inner
    m = _EMAIL_RE.search(s)
    return m.group(0) if m else ""


def _resolve_asset(sdk, name: str) -> str:
    """Best-effort Text-asset read; returns '' if missing/unavailable."""
    if not name:
        return ""
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


def _fallback_note(name: str) -> str:
    """Deterministic acknowledgement note used when the LLM is unavailable."""
    return (
        f"Hi {name} — thanks for submitting your reimbursement request. "
        "It's now in our automated pipeline for review."
    )


def _compose(state: GraphState, sender_name: str, note: str) -> tuple[str, str]:
    name = _greeting_name(state.employee_name, state.employee_email)
    note = _strip_greeting(note)  # avoid a second "Hi {name}," after the greeting line
    amount = _fmt_amount(state.amount, state.currency)
    case = state.case_id or ""

    subject = f"We've received your reimbursement request{f' ({case})' if case else ''}"

    details = "".join(
        [
            _row("Case ID", state.case_id),
            _row("Expense type", state.expense_type),
            _row("Vendor", state.vendor),
            _row("Amount requested", amount if (state.amount or 0) else ""),
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
    <tr><td style="background:{ACCENT};padding:28px 32px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td style="color:#ffffff;font-size:13px;letter-spacing:.5px;text-transform:uppercase;opacity:.9;">{_esc(sender_name)}</td>
          <td align="right"><span style="display:inline-block;background:rgba(255,255,255,.18);color:#ffffff;font-size:11px;font-weight:700;letter-spacing:1px;padding:6px 12px;border-radius:999px;">{BADGE}</span></td>
        </tr>
      </table>
      <div style="color:#ffffff;font-size:24px;font-weight:700;margin-top:18px;">Request received</div>
    </td></tr>
    <!-- intro + note -->
    <tr><td style="padding:32px 32px 8px 32px;">
      <p style="margin:0 0 18px 0;color:#374151;font-size:15px;line-height:1.6;">Hi {_esc(name)},</p>
      <p style="margin:0 0 8px 0;color:#374151;font-size:15px;line-height:1.6;">{_esc(note)}</p>
      <p style="margin:0 0 8px 0;color:#374151;font-size:15px;line-height:1.6;">
        It's being processed automatically — you'll hear back with the verdict in under 5 minutes.
      </p>
    </td></tr>
    <!-- details -->
    <tr><td style="padding:16px 32px 8px 32px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
        {details}
      </table>
    </td></tr>
    <!-- footer -->
    <tr><td style="padding:28px 32px;">
      <hr style="border:none;border-top:1px solid #eceef1;margin:0 0 16px 0;">
      <p style="margin:0;color:#9ca3af;font-size:12px;line-height:1.6;">
        This is an automated message from {_esc(sender_name)} (UiPath reimbursement pipeline).<br>
        Reference: {_esc(state.case_id)}
      </p>
    </td></tr>
  </table>
</td></tr>
</table>
</body>
</html>"""
    return subject, body


# --------------------------------------------------------------------------- #
# Node 1 - write_note (LLM writes PROSE ONLY; deterministic fallback)
# --------------------------------------------------------------------------- #
async def write_note(state: GraphState) -> dict:
    name = _greeting_name(state.employee_name, state.employee_email)
    fallback = _fallback_note(name)

    system_prompt = (
        "You are a thoughtful, professional enterprise reimbursement assistant. Write ONE "
        "short, warm message (max ~25 words) to an employee acknowledging their reimbursement "
        "request was just submitted and is now being processed.\n"
        "STRICT RULES:\n"
        "- Do NOT state how long processing will take (that is shown separately).\n"
        "- Do NOT invent an amount, case id, or verdict.\n"
        "- Do NOT use placeholders or brackets.\n"
        "- Address the employee by first name, naturally.\n"
        "- Output only the message text, nothing else."
    )
    user_prompt = (
        f"Employee first name: {name}\n"
        "Status: reimbursement request just submitted, now processing.\n"
        "Write the message now."
    )

    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from uipath_langchain.chat.models import UiPathChat

        llm = UiPathChat(model=NOTE_MODEL, temperature=0.6, max_tokens=80)
        resp = await llm.ainvoke([SystemMessage(system_prompt), HumanMessage(user_prompt)])
        text = (getattr(resp, "content", "") or "").strip()
        if not text:
            text = fallback
        elif len(text) > 240:
            text = text[:237].rstrip() + "..."
        return {"personal_note": text}
    except Exception:
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

    sender_name = state.sender_name or _resolve_asset(sdk, DEFAULT_SENDER_NAME_ASSET) or FALLBACK_SENDER_NAME
    reply_to = state.sender_email or _resolve_asset(sdk, DEFAULT_REPLY_TO_ASSET)

    note = state.personal_note or _fallback_note(_greeting_name(state.employee_name, state.employee_email))
    subject, body = _compose(state, sender_name, note)

    # Recipient guard: clean+validate to a bare address; fail loud otherwise.
    raw_to = state.employee_email or state.receipt_email or ""
    to = _clean_email(raw_to)
    if not to:
        raise ValueError(
            "No valid recipient email. employee_email/receipt_email did not contain "
            f"a parseable address (got {raw_to.strip()!r}). Ensure the intake form is "
            "carrying the submitter's email address (not their name) into employee_email."
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
    return GraphOutput(
        sent=bool(message_id),
        to=to,
        subject=subject,
        message_id=message_id,
        sender_name=sender_name,
        reply_to=reply_to,
        personal_note=note,
        details=f"{verb} confirmation '{subject}' to {to}",
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
