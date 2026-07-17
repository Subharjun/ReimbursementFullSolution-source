"""
Receipt vision extraction -- "drop a receipt, the form fills itself."

Owner: Subharjun. AgentHack Track 1.

WHY THIS EXISTS
    Manual data entry is the friction in every expense tool. Here the claimant
    drops (or photographs) a receipt and Groq's multimodal model reads it
    directly -- vendor, amount, currency, date, an expense-type guess, a line-item
    summary -- and pre-fills the intake form. The human confirms/edits and
    submits; nothing is auto-submitted, so a misread is caught before it ever
    reaches the Case.

    This is the intake-side companion to consensus/corroborate.py, which later
    cross-checks the SUBMITTED amount/vendor against the receipt text as a fraud
    guardrail. Vision here is convenience (fill the form); corroborate there is
    control (verify the human didn't fudge it). Different jobs, kept separate.

SAFETY / DEGRADATION
    * Advisory only. Every field comes back with the model's own confidence and
      is editable; the form never blocks on it.
    * PDFs are handled by corroborate.extract_text (text layer) since the vision
      model takes images; a scanned-image PDF simply yields no prefill.
    * Any failure (no key, timeout, junk) -> {"ok": False} and the form stays
      manual. No exception escapes.
"""

from __future__ import annotations

from datetime import datetime

from .groq_client import groq_vision_json

# Expense types offered by the intake form (src/IntakeForm.tsx EXPENSE_TYPES).
# These are POLICY CATEGORY KEYS, not display labels: PolicyRuleCheckWorkflow
# indexes its category map with this exact string and silently falls back to
# `others` on any miss. Auto-fill must therefore emit a real key, never a label.
_FORM_TYPES = ["travel", "food", "medical", "internet", "equipment", "others"]
_CURRENCIES = ["INR", "USD", "EUR", "GBP", "AED"]

_IMAGE_MIMES = {
    "image/jpeg": "image/jpeg", "image/jpg": "image/jpeg", "image/png": "image/png",
    "image/webp": "image/webp", "image/heic": "image/jpeg", "image/gif": "image/png",
}

_SYSTEM = """You are a meticulous receipt-reading assistant for a corporate \
reimbursement tool. Read the receipt image and extract ONLY what is actually \
printed on it — never guess or invent a value you cannot see."""

_USER = f"""Extract these fields from the receipt:
- vendor: the merchant/business name (short, as printed)
- amount: the FINAL TOTAL paid, as a number only (no currency symbol, no commas)
- currency: 3-letter ISO code, one of {_CURRENCIES}; infer from the symbol (₹=INR, $=USD, €=EUR, £=GBP). If unclear use "".
- date: the transaction date as YYYY-MM-DD; "" if not legible
- expense_type: the single best fit from {_FORM_TYPES}
- summary: a one-line, human-readable description of what was purchased
- line_items: array of up to 6 {{"desc": str, "amount": number}} if itemized, else []
- confidence: your overall confidence 0.0-1.0 that the total+vendor are correct
- readable: true if this is clearly a receipt you could read, false if blurry/not-a-receipt

Use "" (or null for numbers) for anything you cannot read. Do not fabricate."""


def _mime_for(filename: str | None, given: str | None) -> str | None:
    if given and given.lower() in _IMAGE_MIMES:
        return _IMAGE_MIMES[given.lower()]
    ext = (filename or "").lower().rsplit(".", 1)[-1] if "." in (filename or "") else ""
    return {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "webp": "image/webp", "heic": "image/jpeg", "gif": "image/png"}.get(ext)


def _clean_amount(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        s = str(v).replace(",", "").replace("₹", "").replace("$", "").strip()
        n = float(s)
        return round(n, 2) if n > 0 else None
    except (TypeError, ValueError):
        return None


def _clean_date(v) -> str:
    s = str(v or "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def extract_receipt(raw: bytes | None, filename: str | None,
                    content_type: str | None) -> dict:
    """Read a receipt image and return prefill fields. Never raises."""
    mime = _mime_for(filename, content_type)
    if not raw or not mime:
        return {"ok": False, "reason": "not an image the vision model can read "
                                       "(PDFs and other files are entered manually)"}
    data = groq_vision_json(_SYSTEM, _USER, raw, mime=mime)
    if not data:
        return {"ok": False, "reason": "vision extraction unavailable"}

    amount = _clean_amount(data.get("amount"))
    currency = str(data.get("currency") or "").strip().upper()
    if currency not in _CURRENCIES:
        currency = ""
    # Lowercase before the check: the model is asked for a key but will happily
    # return "Travel". A near-miss should still land on the real category rather
    # than be dropped — but anything not an exact key after folding is dropped,
    # because a wrong key silently buys `others` limits downstream.
    etype = str(data.get("expense_type") or "").strip().lower()
    if etype not in _FORM_TYPES:
        etype = ""
    items = []
    for it in (data.get("line_items") or [])[:6]:
        if isinstance(it, dict) and it.get("desc"):
            items.append({"desc": str(it["desc"])[:80],
                          "amount": _clean_amount(it.get("amount"))})
    try:
        conf = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        conf = 0.0

    fields = {
        "vendor": str(data.get("vendor") or "").strip()[:80],
        "amount": amount,
        "currency": currency,
        "date": _clean_date(data.get("date")),
        "expenseType": etype,
    }
    any_field = any(v for v in fields.values())
    return {
        "ok": bool(data.get("readable", True)) and any_field,
        "readable": bool(data.get("readable", True)),
        "fields": fields,
        "summary": str(data.get("summary") or "").strip()[:200],
        "line_items": items,
        "confidence": round(conf, 2),
        "note": ("Auto-read from your receipt — please confirm before submitting."
                 if any_field else
                 "Couldn't confidently read this receipt — please enter the details manually."),
    }
