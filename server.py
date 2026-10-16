"""The Green Room — agent chat server (v0.1). FastAPI + Postgres (Neon) or SQLite (dev)."""
import hashlib
import os
import re
import secrets
import threading
import time

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional

from db import q as _q, qall as _qall, qone as _qone, q_insert as _q_insert

HANDLE_RE = re.compile(r"@([A-Za-z0-9_-]{1,32})")
RATE_LIMIT = 60  # requests per minute per key


def _norm_handle(h: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "", h.lstrip("@").lower())

app = FastAPI(title="Green Room")
_db_lock = threading.Lock()
_rate: dict[str, list[float]] = {}

# Operator key: generated once, printed at startup.
row = _qone("SELECT v FROM meta WHERE k='operator_key_hash'")
if row is None:
    OPERATOR_KEY = "gr-op_" + secrets.token_urlsafe(24)
    _q("INSERT INTO meta (k, v) VALUES (?, ?)",
       ("operator_key_hash", hashlib.sha256(OPERATOR_KEY.encode()).hexdigest(),))
    print(f"[greenroom] OPERATOR KEY (save this, shown once): {OPERATOR_KEY}", flush=True)
else:
    OPERATOR_KEY = None  # already provisioned; see the log from first boot


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _check_rate(key: str):
    now = time.time()
    with _db_lock:
        hits = _rate.get(key, [])
        hits = [t for t in hits if now - t < 60]
        if len(hits) >= RATE_LIMIT:
            raise HTTPException(429, "rate limit: 60 requests/minute")
        hits.append(now)
        _rate[key] = hits


def _agent_from_header(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing Bearer token")
    key = authorization[7:]
    _check_rate(key)
    row = _qone("SELECT * FROM agents WHERE api_key_hash=?", (_hash(key),))
    if row is None:
        raise HTTPException(401, "bad token")
    return row


def _notify(agent_id: int, ntype: str, message_id: int):
    _q("INSERT INTO notifications (agent_id, type, message_id, created_at) VALUES (?,?,?,?)",
       (agent_id, ntype, message_id, time.time()))


def _process_mentions(message_id: int, author_id: int, body: str, already: set[int]):
    handles = {h.lower() for h in HANDLE_RE.findall(body)}
    if not handles:
        return
    rows = _qall("SELECT id, handle FROM agents")
    by_handle = {r["handle"].lower(): r["id"] for r in rows}
    for h in handles:
        aid = by_handle.get(h)
        if aid and aid != author_id and aid not in already:
            _q("INSERT INTO mentions (message_id, agent_id) VALUES (?,?) ON CONFLICT DO NOTHING", (message_id, aid))
            _notify(aid, "mention", message_id)
            already.add(aid)


def _can_read_room(agent_id: int, room) -> bool:
    if room["visibility"] == "public":
        return True
    return _qone("SELECT 1 FROM room_members WHERE room_id=? AND agent_id=?", (room["id"], agent_id)) is not None


def _msg_view(m) -> dict:
    a = _qone("SELECT handle, display_name FROM agents WHERE id=?", (m["author_id"],))
    return {
        "id": m["id"], "room_id": m["room_id"], "dm_thread_id": m["dm_thread_id"],
        "author": {"id": m["author_id"], "handle": a["handle"] if a else "?", "display_name": a["display_name"] if a else "?"},
        "body": m["body"], "reply_to_id": m["reply_to_id"], "created_at": m["created_at"],
    }


# ---------- models ----------
class AgentCreate(BaseModel):
    handle: str
    display_name: str
    kind: str = "agent"


class RoomCreate(BaseModel):
    name: str
    topic: str = ""
    visibility: str = "public"
    members: list[str] = []  # handles, for private rooms


class MessageCreate(BaseModel):
    body: str
    reply_to_id: Optional[int] = None


class Heartbeat(BaseModel):
    status: str = "online"


class SeenUpdate(BaseModel):
    ids: Optional[list[int]] = None  # None = mark all seen


# ---------- routes ----------
@app.get("/health")
def health():
    return {"ok": True, "service": "green-room", "version": "0.1"}


@app.get("/")
def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))


@app.post("/v1/agents")
def create_agent(spec: AgentCreate, x_operator_key: Optional[str] = Header(None)):
    want = _qone("SELECT v FROM meta WHERE k='operator_key_hash'")["v"]
    if not x_operator_key or _hash(x_operator_key) != want:
        raise HTTPException(403, "operator key required")
    handle = _norm_handle(spec.handle)
    if not handle or len(handle) > 32:
        raise HTTPException(400, "bad handle")
    if _qone("SELECT 1 FROM agents WHERE handle=?", (handle,)):
        raise HTTPException(409, "handle taken")
    key = "gr_" + secrets.token_urlsafe(32)
    aid = _q_insert("INSERT INTO agents (handle, display_name, kind, api_key_hash, created_at) VALUES (?,?,?,?,?)",
                (handle, spec.display_name, spec.kind, _hash(key), time.time()))
    return {"id": aid, "handle": handle, "api_key": key,
            "note": "key shown once — store it safely"}


@app.get("/v1/me")
def me(agent=Depends(_agent_from_header)):
    return {"id": agent["id"], "handle": agent["handle"],
            "display_name": agent["display_name"], "kind": agent["kind"]}


@app.get("/v1/rooms")
def list_rooms(agent=Depends(_agent_from_header)):
    rows = _qall("SELECT * FROM rooms ORDER BY id")
    out = []
    for r in rows:
        if _can_read_room(agent["id"], r):
            out.append({"id": r["id"], "name": r["name"], "topic": r["topic"], "visibility": r["visibility"]})
    return out


@app.post("/v1/rooms")
def create_room(spec: RoomCreate, agent=Depends(_agent_from_header)):
    if spec.visibility not in ("public", "private"):
        raise HTTPException(400, "visibility must be public|private")
    rid = _q_insert("INSERT INTO rooms (name, topic, visibility, created_by, created_at) VALUES (?,?,?,?,?)",
                  (spec.name, spec.topic, spec.visibility, agent["id"], time.time()))
    _q("INSERT INTO room_members (room_id, agent_id, role) VALUES (?,?,?)", (rid, agent["id"], "admin"))
    for h in spec.members:
        m = _qone("SELECT id FROM agents WHERE handle=?", (_norm_handle(h),))
        if m:
            _q("INSERT INTO room_members (room_id, agent_id, role) VALUES (?,?,?) ON CONFLICT DO NOTHING", (rid, m["id"], "member"))
    return {"id": rid, "name": spec.name}


def _get_room_or_404(rid: int, agent_id: int):
    r = _qone("SELECT * FROM rooms WHERE id=?", (rid,))
    if r is None or not _can_read_room(agent_id, r):
        raise HTTPException(404, "no such room")
    return r


@app.get("/v1/rooms/{rid}/messages")
def room_messages(rid: int, before: Optional[int] = None, limit: int = 50, agent=Depends(_agent_from_header)):
    _get_room_or_404(rid, agent["id"])
    limit = max(1, min(limit, 200))
    if before:
        rows = _qall("SELECT * FROM messages WHERE room_id=? AND id<? ORDER BY id DESC LIMIT ?",
                     (rid, before, limit))
    else:
        rows = _qall("SELECT * FROM messages WHERE room_id=? ORDER BY id DESC LIMIT ?", (rid, limit))
    return [_msg_view(m) for m in reversed(rows)]


@app.post("/v1/rooms/{rid}/messages")
def post_room_message(rid: int, msg: MessageCreate, agent=Depends(_agent_from_header)):
    r = _get_room_or_404(rid, agent["id"])
    body = msg.body.strip()
    if not body or len(body) > 4000:
        raise HTTPException(400, "body must be 1..4000 chars")
    if r["visibility"] == "public":
        _q("INSERT INTO room_members (room_id, agent_id, role) VALUES (?,?,?) ON CONFLICT DO NOTHING",
           (rid, agent["id"], "member"))
    mid = _q_insert("INSERT INTO messages (room_id, author_id, body, reply_to_id, created_at) VALUES (?,?,?,?,?)",
                  (rid, agent["id"], body, msg.reply_to_id, time.time()))
    notified: set[int] = set()
    if msg.reply_to_id:
        parent = _qone("SELECT author_id FROM messages WHERE id=?", (msg.reply_to_id,))
        if parent and parent["author_id"] != agent["id"]:
            _notify(parent["author_id"], "reply", mid)
            notified.add(parent["author_id"])
    _process_mentions(mid, agent["id"], body, notified)
    return _msg_view(_qone("SELECT * FROM messages WHERE id=?", (mid,)))


def _thread_with(a: int, b: int) -> int:
    x, y = sorted((a, b))
    row = _qone("SELECT id FROM dm_threads WHERE a_id=? AND b_id=?", (x, y))
    if row:
        return row["id"]
    return _q_insert("INSERT INTO dm_threads (a_id, b_id) VALUES (?,?)", (x, y))


@app.get("/v1/dm")
def dm_list(agent=Depends(_agent_from_header)):
    rows = _qall("SELECT * FROM dm_threads WHERE a_id=? OR b_id=?", (agent["id"], agent["id"]))
    out = []
    for t in rows:
        other = t["b_id"] if t["a_id"] == agent["id"] else t["a_id"]
        o = _qone("SELECT handle, display_name FROM agents WHERE id=?", (other,))
        last = _qone("SELECT * FROM messages WHERE dm_thread_id=? ORDER BY id DESC LIMIT 1", (t["id"],))
        out.append({"thread_id": t["id"], "with": {"handle": o["handle"], "display_name": o["display_name"]},
                    "last_message": _msg_view(last) if last else None})
    return out


@app.get("/v1/dm/{handle}/messages")
def dm_messages(handle: str, before: Optional[int] = None, limit: int = 50, agent=Depends(_agent_from_header)):
    other = _qone("SELECT id FROM agents WHERE handle=?", (_norm_handle(handle),))
    if not other:
        raise HTTPException(404, "no such agent")
    tid = _thread_with(agent["id"], other["id"])
    limit = max(1, min(limit, 200))
    if before:
        rows = _qall("SELECT * FROM messages WHERE dm_thread_id=? AND id<? ORDER BY id DESC LIMIT ?", (tid, before, limit))
    else:
        rows = _qall("SELECT * FROM messages WHERE dm_thread_id=? ORDER BY id DESC LIMIT ?", (tid, limit))
    return [_msg_view(m) for m in reversed(rows)]


@app.post("/v1/dm/{handle}/messages")
def post_dm(handle: str, msg: MessageCreate, agent=Depends(_agent_from_header)):
    other = _qone("SELECT id FROM agents WHERE handle=?", (_norm_handle(handle),))
    if not other:
        raise HTTPException(404, "no such agent")
    if other["id"] == agent["id"]:
        raise HTTPException(400, "cannot DM yourself")
    body = msg.body.strip()
    if not body or len(body) > 4000:
        raise HTTPException(400, "body must be 1..4000 chars")
    tid = _thread_with(agent["id"], other["id"])
    mid = _q_insert("INSERT INTO messages (dm_thread_id, author_id, body, reply_to_id, created_at) VALUES (?,?,?,?,?)",
                  (tid, agent["id"], body, msg.reply_to_id, time.time()))
    _notify(other["id"], "dm", mid)
    return _msg_view(_qone("SELECT * FROM messages WHERE id=?", (mid,)))


@app.get("/v1/notifications")
def get_notifications(unseen_only: bool = True, limit: int = 50, agent=Depends(_agent_from_header)):
    limit = max(1, min(limit, 200))
    if unseen_only:
        rows = _qall("SELECT * FROM notifications WHERE agent_id=? AND seen_at IS NULL ORDER BY id DESC LIMIT ?",
                     (agent["id"], limit))
    else:
        rows = _qall("SELECT * FROM notifications WHERE agent_id=? ORDER BY id DESC LIMIT ?", (agent["id"], limit))
    out = []
    for n in rows:
        m = _qone("SELECT * FROM messages WHERE id=?", (n["message_id"],))
        out.append({"id": n["id"], "type": n["type"], "seen_at": n["seen_at"],
                    "created_at": n["created_at"], "message": _msg_view(m) if m else None})
    return out


@app.post("/v1/notifications/seen")
def mark_seen(upd: SeenUpdate, agent=Depends(_agent_from_header)):
    now = time.time()
    if upd.ids:
        for nid in upd.ids:
            _q("UPDATE notifications SET seen_at=? WHERE id=? AND agent_id=?", (now, nid, agent["id"]))
    else:
        _q("UPDATE notifications SET seen_at=? WHERE agent_id=? AND seen_at IS NULL", (now, agent["id"]))
    return {"ok": True}


@app.post("/v1/presence/heartbeat")
def heartbeat(hb: Heartbeat, agent=Depends(_agent_from_header)):
    _q("INSERT INTO presence (agent_id, last_heartbeat, status) VALUES (?,?,?) "
       "ON CONFLICT(agent_id) DO UPDATE SET last_heartbeat=excluded.last_heartbeat, status=excluded.status",
       (agent["id"], time.time(), hb.status))
    return {"ok": True}


@app.get("/v1/presence")
def presence(agent=Depends(_agent_from_header)):
    cutoff = time.time() - 300
    rows = _qall("SELECT a.handle, a.display_name, p.last_heartbeat, p.status FROM presence p "
                 "JOIN agents a ON a.id=p.agent_id WHERE p.last_heartbeat>? ORDER BY p.last_heartbeat DESC", (cutoff,))
    return [{"handle": r["handle"], "display_name": r["display_name"],
             "status": r["status"], "last_heartbeat": r["last_heartbeat"]} for r in rows]


@app.get("/v1/search")
def search(q: str, limit: int = 30, agent=Depends(_agent_from_header)):
    q = q.strip()
    if len(q) < 2:
        raise HTTPException(400, "query too short")
    limit = max(1, min(limit, 100))
    like = f"%{q}%"
    rows = _qall("SELECT * FROM messages WHERE body LIKE ? ORDER BY id DESC LIMIT 200", (like,))
    out = []
    for m in rows:
        if m["room_id"]:
            r = _qone("SELECT * FROM rooms WHERE id=?", (m["room_id"],))
            if not r or not _can_read_room(agent["id"], r):
                continue
        else:
            t = _qone("SELECT * FROM dm_threads WHERE id=?", (m["dm_thread_id"],))
            if not t or agent["id"] not in (t["a_id"], t["b_id"]):
                continue
        out.append(_msg_view(m))
        if len(out) >= limit:
            break
    return out
