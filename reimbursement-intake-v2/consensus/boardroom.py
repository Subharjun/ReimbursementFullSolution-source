"""
Boardroom -- the live event bus behind the streamed multi-agent debate.

Owner: Subharjun. AgentHack Track 1.

WHY THIS EXISTS
    The Consensus Engine used to be a black box for ~140s (four sequential live
    Orchestrator jobs) and then a finished transcript appeared. The Boardroom
    turns that dead air into the show itself: every stage start/finish and every
    debate turn is emitted as an event the moment it happens, and /dashboard
    streams them over SSE -- so a viewer literally watches the agents convene,
    argue, and rule in real time.

HOW IT WORKS
    One `_Session` per case_id: a replay buffer (late joiners get the full story
    so far) + a set of per-subscriber queues (live listeners get each new event
    the instant it's emitted). `emit()` is called from the debate thread via the
    `on_event` hook in `run_debate`; `subscribe()` is consumed by the SSE
    endpoint. Everything is thread-safe and bounded -- a session that nobody
    watches costs one small list, and sessions expire after _SESSION_TTL.

    Event shape (what goes over the wire, one JSON object per SSE `data:` line):
        {"seq": n, "at": epoch, "type": "...", ...payload}
    Types: convened, stage_started, stage_done, turn, verdict, adjourned.
"""

from __future__ import annotations

import queue
import threading
import time

_SESSION_TTL = 60 * 30          # forget a boardroom session after 30 min
_MAX_REPLAY = 200               # events kept for late joiners, per session
_MAX_SESSIONS = 40


class _Session:
    def __init__(self, case_id: str):
        self.case_id = case_id
        self.created = time.time()
        self.lock = threading.Lock()
        self.events: list[dict] = []          # replay buffer
        self.subscribers: set[queue.Queue] = set()
        self.closed = False


_LOCK = threading.Lock()
_SESSIONS: dict[str, _Session] = {}


def _session(case_id: str, create: bool = True) -> _Session | None:
    with _LOCK:
        s = _SESSIONS.get(case_id)
        if s is None and create:
            s = _SESSIONS[case_id] = _Session(case_id)
            # opportunistic GC: drop expired / excess sessions
            now = time.time()
            stale = [k for k, v in _SESSIONS.items()
                     if now - v.created > _SESSION_TTL and k != case_id]
            for k in stale:
                _SESSIONS.pop(k, None)
            while len(_SESSIONS) > _MAX_SESSIONS:
                oldest = min(_SESSIONS.values(), key=lambda v: v.created)
                if oldest.case_id == case_id:
                    break
                _SESSIONS.pop(oldest.case_id, None)
        return s


def emit(case_id: str, event_type: str, payload: dict | None = None) -> None:
    """Publish one boardroom event. Never raises; never blocks the debate."""
    try:
        s = _session(case_id)
        if s is None or s.closed:
            return
        with s.lock:
            ev = {"seq": len(s.events), "at": round(time.time(), 3),
                  "type": event_type, **(payload or {})}
            s.events.append(ev)
            del s.events[:-_MAX_REPLAY]
            if event_type == "adjourned":
                s.closed = True
            for q in list(s.subscribers):
                try:
                    q.put_nowait(ev)
                except queue.Full:
                    pass  # a slow client loses an event; the replay buffer has it
    except Exception:
        pass


def subscribe(case_id: str):
    """Generator of boardroom events: full replay first, then live until
    `adjourned` (or a 25s heartbeat gap keeps proxies from cutting the SSE)."""
    s = _session(case_id)
    q: queue.Queue = queue.Queue(maxsize=500)
    with s.lock:
        replay = list(s.events)
        already_closed = s.closed
        if not already_closed:
            s.subscribers.add(q)
    try:
        for ev in replay:
            yield ev
        if already_closed:
            return
        idle_since = time.time()
        while True:
            try:
                ev = q.get(timeout=5.0)
                idle_since = time.time()
                yield ev
                if ev.get("type") == "adjourned":
                    return
            except queue.Empty:
                # SSE comment heartbeat so Render/Cloudflare don't drop the pipe
                yield {"type": "_heartbeat"}
                if time.time() - idle_since > _SESSION_TTL:
                    return
    finally:
        with s.lock:
            s.subscribers.discard(q)


def latest_session_id() -> str | None:
    """case_id of the most recently convened boardroom (for 'watch live' UX)."""
    with _LOCK:
        if not _SESSIONS:
            return None
        return max(_SESSIONS.values(), key=lambda v: v.created).case_id
