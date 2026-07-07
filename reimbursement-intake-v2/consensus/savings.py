"""ROI / savings model for the reimbursement pipeline.

Owner: Subharjun.

Every processed claim carries a *quantified* impact figure derived from
published Accounts-Payable benchmarks, so the pipeline demonstrates measurable
**value**, not just automation. Invoice/receipt reading is commoditized; the
differentiator is showing, per transaction, what the agentic pipeline saved
over a manual + paper-check process — with transparent, tunable assumptions
(nothing is a black box: `compute_savings()` returns the `assumptions` it used
alongside every figure).

Benchmark basis (industry figures, NOT our own measurements):
  - Manual invoice handling ~10-25 min each; all-in manual processing cost
    ~$10-12 vs ~$2-3 automated  (Ardent Partners / APQC AP benchmarks).
  - Paper-check all-in cost ~$4-6+; a digital/virtual payout is a fraction of
    that                         (AP automation benchmarks).
  - ~0.1-2% of invoices are duplicate / erroneous payments
                                 (IOFM / Ardent Partners).
  - Early-payment terms (e.g. 2/10 net 30) ≈ 2% capturable when approval is
    fast enough to hit the window.

All operational cost benchmarks are USD (that is how the source studies quote
them); amount-derived figures (duplicate loss prevented, discount captured) are
in the claim's own currency. Every constant is overridable via an env var so
Finance can re-anchor the model to their real numbers without touching code.
"""

import os


def _envf(name: str, default: float) -> float:
    """Read a float override from the environment, falling back to `default`."""
    try:
        return float(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


# --- effort / cost benchmarks (USD, tunable) --------------------------------- #
MANUAL_MINUTES_PER_CLAIM = _envf("ROI_MANUAL_MINUTES", 18.0)      # 10-25 min typical
AUTOMATED_MINUTES_PER_CLAIM = _envf("ROI_AUTOMATED_MINUTES", 0.5)  # agent path ~seconds
MANUAL_COST_USD = _envf("ROI_MANUAL_COST_USD", 12.0)              # all-in manual /invoice
AUTOMATED_COST_USD = _envf("ROI_AUTOMATED_COST_USD", 2.5)         # all-in automated /invoice
CHECK_COST_USD = _envf("ROI_CHECK_COST_USD", 6.0)                 # paper-check all-in
DIGITAL_PAYOUT_COST_USD = _envf("ROI_DIGITAL_PAYOUT_COST_USD", 0.5)  # ACH / virtual card

# --- amount-derived rates (fraction of the claim, tunable) ------------------- #
DUPLICATE_BASE_RATE = _envf("ROI_DUPLICATE_BASE_RATE", 0.005)     # 0.5% baseline dup rate
EARLY_PAY_DISCOUNT_RATE = _envf("ROI_DISCOUNT_RATE", 0.02)        # 2/10 net 30 ≈ 2%


def compute_savings(
    amount: float,
    currency: str = "USD",
    duplicate_detected: bool = False,
    payout_method: str = "digital",
    discount_eligible: bool = False,
) -> dict:
    """Return the quantified savings for a single processed claim.

    Figures:
      - `minutes_saved`         : manual effort avoided (min).
      - `processing_cost_saved` : USD, manual vs automated per-claim cost.
      - `payout_cost_saved`     : USD, paper-check vs digital payout (0 if the
                                  payout was not digital).
      - `operational_saved_usd` : processing + payout cost saved (USD).
      - `duplicate_loss_prevented` : claim currency. The FULL amount when a
                                  duplicate was actually caught (a real double
                                  payment averted); otherwise the statistical
                                  expected loss avoided by verifying it wasn't a
                                  duplicate (amount x baseline dup rate).
      - `duplicate_caught`      : bool — was this claim flagged as a duplicate.
      - `discount_captured`     : claim currency, early-payment discount unlocked
                                  by fast approval (0 unless eligible).
      - `currency`, `assumptions` : for transparent display.
    """
    try:
        amt = max(0.0, float(amount or 0))
    except (TypeError, ValueError):
        amt = 0.0
    cur = (currency or "USD").upper()
    is_digital = (payout_method or "digital").lower() not in {"check", "cheque", "paper"}

    minutes_saved = max(0.0, MANUAL_MINUTES_PER_CLAIM - AUTOMATED_MINUTES_PER_CLAIM)
    processing_cost_saved = max(0.0, MANUAL_COST_USD - AUTOMATED_COST_USD)
    payout_cost_saved = max(0.0, CHECK_COST_USD - DIGITAL_PAYOUT_COST_USD) if is_digital else 0.0
    operational_saved_usd = round(processing_cost_saved + payout_cost_saved, 2)

    if duplicate_detected:
        duplicate_loss_prevented = round(amt, 2)          # a real double-pay averted
    else:
        duplicate_loss_prevented = round(amt * DUPLICATE_BASE_RATE, 2)  # expected value

    discount_captured = round(amt * EARLY_PAY_DISCOUNT_RATE, 2) if discount_eligible else 0.0

    return {
        "currency": cur,
        "minutes_saved": round(minutes_saved, 1),
        "processing_cost_saved": round(processing_cost_saved, 2),
        "payout_cost_saved": round(payout_cost_saved, 2),
        "operational_saved_usd": operational_saved_usd,
        "duplicate_caught": bool(duplicate_detected),
        "duplicate_loss_prevented": duplicate_loss_prevented,
        "discount_captured": discount_captured,
        "assumptions": {
            "manual_minutes_per_claim": MANUAL_MINUTES_PER_CLAIM,
            "manual_cost_usd": MANUAL_COST_USD,
            "automated_cost_usd": AUTOMATED_COST_USD,
            "check_cost_usd": CHECK_COST_USD,
            "digital_payout_cost_usd": DIGITAL_PAYOUT_COST_USD,
            "duplicate_base_rate": DUPLICATE_BASE_RATE,
            "early_pay_discount_rate": EARLY_PAY_DISCOUNT_RATE,
            "basis": "Ardent Partners / APQC / IOFM AP benchmarks (industry, illustrative)",
        },
    }


def summary_line(savings: dict) -> str:
    """One-line plain-text ROI summary (for logs / the agent output `details`)."""
    parts = [
        f"~{savings['minutes_saved']:.0f} min manual effort saved",
        f"~${savings['operational_saved_usd']:,.2f} processing+payout cost saved",
    ]
    if savings["duplicate_caught"]:
        parts.append(
            f"DUPLICATE CAUGHT — {savings['duplicate_loss_prevented']:,.2f} "
            f"{savings['currency']} double-payment prevented"
        )
    if savings["discount_captured"] > 0:
        parts.append(
            f"{savings['discount_captured']:,.2f} {savings['currency']} early-pay discount captured"
        )
    return " | ".join(parts)
