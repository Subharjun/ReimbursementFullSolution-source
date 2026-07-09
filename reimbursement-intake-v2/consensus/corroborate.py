"""
Receipt corroboration -- does the uploaded receipt actually back the typed form? (#2)

Owner: Subharjun. AgentHack Track 1.

WHY THIS EXISTS
    The Case declares an `ocrConfidence` input and its Guardrail stage branches on
    it, but the intake app used to hard-code `ocrConfidence = 1.0` on every
    submission -- i.e. it always told the Case "the OCR was perfect" even when no
    receipt was attached or the receipt didn't support the typed numbers. This
    module computes a REAL confidence by cross-checking the typed amount / vendor
    against text actually extracted from the uploaded receipt, and surfaces any
    mismatch (e.g. "you typed 500 but the receipt reads 1,500") BEFORE payout.

    It is deliberately dependency-light and never throws: if the file is a scanned
    image (no text layer) or no PDF text extractor is installed, it degrades to an
    honest "attached but not machine-verified" instead of pretending OCR succeeded.
    That honesty is the point -- the Guardrail should see the truth, not a 1.0
    placeholder.

    NB: the Case's own ReceiptExtractor stage still does the authoritative in-
    pipeline OCR. This is a fast, pre-flight corroboration so the guardrail input
    is truthful and gross form/receipt mismatches are caught at the door.
"""

from __future__ import annotations

import re
from typing import Any


# --------------------------------------------------------------------------- #
# Text extraction (best-effort, multiple backends, zero hard dependency)
# --------------------------------------------------------------------------- #
def _looks_like_pdf(raw: bytes) -> bool:
    return raw[:5] == b"%PDF-"


def extract_text(raw: bytes, filename: str) -> str | None:
    """Return extracted text, or None if this file can't be text-extracted here.

    Handles PDFs with a text layer (via pypdf or PyPDF2, whichever is present) and
    plain-text uploads. Scanned-image receipts (JP/PNG) return None -- we do not
    ship an image-OCR engine in the app; the Case's ReceiptExtractor owns that.
    """
    if not raw:
        return None
    name = (filename or "").lower()

    if name.endswith((".txt", ".csv")) or (not _looks_like_pdf(raw) and not name.endswith((".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif"))):
        try:
            return raw.decode("utf-8", errors="ignore")
        except Exception:
            return None

    if _looks_like_pdf(raw) or name.endswith(".pdf"):
        text = _extract_pdf_text(raw)
        return text or None  # empty (scanned/no text layer) -> None (honest "unverified")

    return None  # image -> not verifiable here


def _extract_pdf_text(raw: bytes) -> str:
    import io

    # Try pypdf first (modern), then PyPDF2 (legacy) -- whichever is installed.
    for mod_name, reader_attr in (("pypdf", "PdfReader"), ("PyPDF2", "PdfReader")):
        try:
            mod = __import__(mod_name)
            reader = getattr(mod, reader_attr)(io.BytesIO(raw))
            parts = []
            for page in reader.pages:
                try:
                    parts.append(page.extract_text() or "")
                except Exception:
                    continue
            return "\n".join(parts).strip()
        except Exception:
            continue
    return ""


# --------------------------------------------------------------------------- #
# Cross-check helpers
# --------------------------------------------------------------------------- #
def _norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers_in(text: str) -> list[float]:
    out: list[float] = []
    for m in _NUM_RE.finditer(text or ""):
        try:
            out.append(float(m.group(0).replace(",", "")))
        except ValueError:
            continue
    return out


def _amount_present(text: str, amount: float) -> bool:
    if amount <= 0:
        return False
    return any(abs(n - amount) <= 0.01 for n in _numbers_in(text))


def _vendor_present(text: str, vendor: str) -> bool:
    v = _norm(vendor)
    if len(v) < 3:
        return False
    t = _norm(text)
    if v in t:
        return True
    # Fall back to the longest word-token of the vendor name (handles "Cafe Rio
    # Downtown LLC" vs a receipt header that only says "CAFE RIO").
    tokens = [tok for tok in re.split(r"[^a-z0-9]+", str(vendor or "").lower()) if len(tok) >= 4]
    return any(tok in t for tok in tokens)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def corroborate_receipt(raw: bytes | None, filename: str | None, claim: dict) -> dict:
    """Cross-check the typed claim against the uploaded receipt.

    Returns:
      { ocr_confidence: float 0..1,        # REAL confidence for the Case guardrail
        corroborated: bool | None,         # True/False if verifiable, None if not
        amount_found: bool, vendor_found: bool,
        method: "pdf-text" | "text" | "image-unverified" | "no-receipt",
        mismatches: [str], note: str }
    """
    amount = _to_float(claim.get("amount") or claim.get("expenseAmount"))
    vendor = claim.get("vendor") or claim.get("expenseVendor") or ""

    if not raw:
        return {
            "ocr_confidence": 0.0, "corroborated": None,
            "amount_found": False, "vendor_found": False,
            "method": "no-receipt", "mismatches": ["No receipt was attached."],
            "note": "No receipt to corroborate — the guardrail should treat this as unverified.",
        }

    text = extract_text(raw, filename or "")
    if not text:
        # Scanned image or no text layer: honestly unverified, neutral-low, not 1.0.
        return {
            "ocr_confidence": 0.55, "corroborated": None,
            "amount_found": False, "vendor_found": False,
            "method": "image-unverified",
            "mismatches": [],
            "note": ("Receipt attached but not machine-verifiable in pre-flight "
                     "(image or no text layer); the Case ReceiptExtractor will OCR it."),
        }

    amount_found = _amount_present(text, amount)
    vendor_found = _vendor_present(text, vendor)

    conf = 0.50
    conf += 0.35 if amount_found else 0.0
    conf += 0.15 if vendor_found else 0.0
    mismatches: list[str] = []
    if not amount_found:
        conf = min(conf, 0.45)
        mismatches.append(
            f"The typed amount ({amount:,.2f}) was not found on the receipt text."
        )
    if not vendor_found and vendor:
        mismatches.append(f"The typed vendor ('{vendor}') was not found on the receipt text.")

    corroborated = amount_found  # the amount is the signal that matters for payout
    method = "pdf-text" if _looks_like_pdf(raw) else "text"
    note = (
        "Receipt corroborates the typed amount"
        + (" and vendor." if vendor_found else " (vendor not matched).")
        if amount_found else
        "Receipt text does NOT corroborate the typed amount — worth a human glance."
    )
    return {
        "ocr_confidence": round(min(1.0, conf), 2),
        "corroborated": corroborated,
        "amount_found": amount_found,
        "vendor_found": vendor_found,
        "method": method,
        "mismatches": mismatches,
        "note": note,
    }


def _to_float(v: Any) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(v or ""))
    if not m:
        return 0.0
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return 0.0
