"""
Decision Ledger -- a tamper-evident, hash-chained record of every decision the
system makes about money.

Owner: Subharjun. AgentHack Track 1.

WHY THIS EXISTS
    "The AI decided" is not an audit trail. The ledger makes the whole decision
    provenance VERIFIABLE: every material event (claim submitted, committee
    verdict, human decision, payout dispatched) is appended as a block whose
    hash covers its payload AND the previous block's hash -- so retro-editing
    any historical decision breaks the chain visibly. `verify()` re-walks the
    chain and reports the first broken link, and /ledger renders it publicly.

    This is blockchain-shaped without the theater: one process, one file, no
    consensus protocol -- exactly enough cryptography to make tampering
    *evident*, which is what an expense audit actually needs.

DURABILITY
    Blocks are held in memory and appended to LEDGER_PATH (JSONL) so a local
    run survives restarts. On Render the filesystem is ephemeral -- the chain
    restarts with the dyno, which is honest: it never claims history it can't
    prove. Every block says what it covers; the genesis block records when the
    chain began.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time

LEDGER_PATH = os.environ.get("LEDGER_PATH", "decision_ledger.jsonl")
_MAX_BLOCKS = 2000

_LOCK = threading.Lock()
_CHAIN: list[dict] = []


def _digest(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _block_hash(block: dict) -> str:
    """Hash covers everything except the hash field itself."""
    core = {k: v for k, v in block.items() if k != "hash"}
    return _digest(core)


def _genesis() -> dict:
    b = {
        "index": 0, "at": round(time.time(), 3), "kind": "genesis",
        "case_id": None,
        "payload": {"note": "Decision Ledger chain begins here. Every block's "
                            "hash covers its payload and the previous hash — "
                            "editing history breaks the chain visibly."},
        "prev_hash": "0" * 64,
    }
    b["hash"] = _block_hash(b)
    return b


def _load() -> None:
    """Restore the chain from disk (best-effort; a corrupt tail is dropped)."""
    if not os.path.exists(LEDGER_PATH):
        _CHAIN.append(_genesis())
        return
    try:
        with open(LEDGER_PATH) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                blk = json.loads(line)
                if _CHAIN and blk.get("prev_hash") != _CHAIN[-1]["hash"]:
                    break  # broken tail: keep the verified prefix
                if _block_hash(blk) != blk.get("hash"):
                    break
                _CHAIN.append(blk)
    except Exception:
        pass
    if not _CHAIN:
        _CHAIN.append(_genesis())


def _persist(block: dict) -> None:
    try:
        with open(LEDGER_PATH, "a") as fh:
            fh.write(json.dumps(block, default=str) + "\n")
    except Exception:
        pass  # in-memory chain remains authoritative for this process


def record(kind: str, case_id: str | None, payload: dict) -> dict:
    """Append one block. kind: submission | committee_verdict | human_decision |
    payout | note. Returns the block (with its hash). Never raises."""
    with _LOCK:
        if not _CHAIN:
            _load()
        prev = _CHAIN[-1]
        block = {
            "index": prev["index"] + 1,
            "at": round(time.time(), 3),
            "kind": kind,
            "case_id": case_id,
            "payload": payload,
            "prev_hash": prev["hash"],
        }
        block["hash"] = _block_hash(block)
        _CHAIN.append(block)
        del _CHAIN[:-_MAX_BLOCKS]
        _persist(block)
        return block


def chain(limit: int = 200) -> list[dict]:
    with _LOCK:
        if not _CHAIN:
            _load()
        return list(_CHAIN[-limit:])


def verify() -> dict:
    """Re-walk the whole in-memory chain. Reports the first broken link, if any."""
    with _LOCK:
        if not _CHAIN:
            _load()
        snapshot = list(_CHAIN)
    for i, blk in enumerate(snapshot):
        if _block_hash(blk) != blk.get("hash"):
            return {"ok": False, "blocks": len(snapshot), "broken_at": blk["index"],
                    "reason": "block content does not match its hash (payload edited)"}
        if i > 0 and blk.get("prev_hash") != snapshot[i - 1]["hash"]:
            return {"ok": False, "blocks": len(snapshot), "broken_at": blk["index"],
                    "reason": "prev_hash does not match the previous block (chain spliced)"}
    return {"ok": True, "blocks": len(snapshot), "broken_at": None,
            "head_hash": snapshot[-1]["hash"] if snapshot else None}
