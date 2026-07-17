"""
Fraud Constellation -- the claim-link network behind the duplicate/split-claim
detectors, as a renderable graph.

Owner: Subharjun. AgentHack Track 1.

WHY THIS EXISTS
    The detectors already reason over relationships (same vendor, near-identical
    amounts, tight date windows) but reported them one claim at a time. The
    constellation view exposes the whole graph at once: claims are stars, and
    the suspicious relationships are the lines between them -- a duplicate ring
    literally lights up as a connected cluster on /dashboard.

    Same thresholds as detectors.py, same data (the real claim history the
    debates screen against). Read-only.
"""

from __future__ import annotations

from datetime import datetime

from .agents import _to_float

# Mirror detectors.py's windows so the graph shows the SAME relationships the
# fraud stage scores (a judge can cross-check an edge against a Rex flag).
_DUP_WINDOW_DAYS = 7
_SPLIT_WINDOW_DAYS = 3
_NEAR_AMOUNT_TOL = 0.02      # 2% = "near-identical amount"


def _norm(s) -> str:
    return " ".join(str(s or "").lower().split())


def _date(v) -> datetime | None:
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(str(v)[:10], fmt)
        except Exception:
            continue
    return None


def _days(a, b) -> int | None:
    if a is None or b is None:
        return None
    return abs((a - b).days)


def build_graph(records: list[dict]) -> dict:
    """records: debate records (each carries `claim` + verdict) and/or bare
    claims. Returns {nodes, edges, clusters} for the constellation canvas."""
    nodes: list[dict] = []
    seen: set[str] = set()
    for r in records:
        claim = r.get("claim") or r
        cid = str(r.get("case_id") or claim.get("case_id") or f"claim-{len(nodes)}")
        if cid in seen:
            continue
        seen.add(cid)
        nodes.append({
            "id": cid,
            "vendor": claim.get("vendor") or "(unknown)",
            "amount": _to_float(claim.get("amount")),
            "currency": claim.get("currency") or "",
            "expense_type": claim.get("expense_type") or "",
            "date": claim.get("date"),
            "verdict": r.get("recommendation"),
            "family": _fam(r.get("recommendation")),
        })

    edges: list[dict] = []
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            a, b = nodes[i], nodes[j]
            rel = _relate(a, b)
            if rel:
                edges.append({"source": a["id"], "target": b["id"], **rel})

    # Connected components over suspicious edges = the "rings".
    strong = [(e["source"], e["target"]) for e in edges if e["kind"] != "same_vendor"]
    clusters = _components({n["id"] for n in nodes}, strong)
    suspicious_clusters = [c for c in clusters if len(c) >= 2]
    return {
        "nodes": nodes,
        "edges": edges,
        "rings": [sorted(c) for c in suspicious_clusters],
        "generated_by": (
            "Claim-link graph over the same history the FraudIntegrityAgent "
            "screens against, using the detectors' own duplicate/split windows. "
            "Read-only analysis; nothing dispatched."
        ),
    }


def _relate(a: dict, b: dict) -> dict | None:
    """The strongest suspicious relationship between two claims, if any."""
    same_vendor = _norm(a["vendor"]) == _norm(b["vendor"]) and _norm(a["vendor"])
    if not same_vendor:
        return None
    da, db = _date(a["date"]), _date(b["date"])
    gap = _days(da, db)
    amt_a, amt_b = a["amount"], b["amount"]
    near_amount = (
        amt_a > 0 and amt_b > 0
        and abs(amt_a - amt_b) <= _NEAR_AMOUNT_TOL * max(amt_a, amt_b)
    )
    if near_amount and gap is not None and gap <= _DUP_WINDOW_DAYS:
        return {"kind": "possible_duplicate", "weight": 3,
                "why": f"same vendor, amounts within 2%, {gap}d apart"}
    if gap is not None and gap <= _SPLIT_WINDOW_DAYS:
        return {"kind": "possible_split", "weight": 2,
                "why": f"same vendor {gap}d apart (split-claim window)"}
    return {"kind": "same_vendor", "weight": 1, "why": "same vendor"}


def _components(node_ids: set[str], links: list[tuple[str, str]]) -> list[set[str]]:
    parent = {n: n for n in node_ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in links:
        if a in parent and b in parent:
            parent[find(a)] = find(b)
    comps: dict[str, set[str]] = {}
    for n in node_ids:
        comps.setdefault(find(n), set()).add(n)
    return list(comps.values())


def _fam(verdict) -> str:
    v = (verdict or "").lower()
    if "reject" in v:
        return "reject"
    if "approve" in v or "proceed" in v:
        return "approve"
    if v:
        return "review"
    return "unknown"
