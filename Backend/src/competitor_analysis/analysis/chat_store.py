"""SQLite persistence for chat sessions.

Each session holds its messages (text plus the UI payload: tool chips, charts,
tables, verification, citations) and a small `state` dict - the conversation's
working focus (companies, metrics, period) - which the agent folds into its
system prompt so follow-ups like "what about their combined ratio?" resolve.

One short-lived connection per call: the chat endpoint runs the agent in a
worker thread, and sqlite3 connections must not cross threads.
"""
import json
import sqlite3
import time
from contextlib import closing

from competitor_analysis import paths

DB_FILE = paths.ARTIFACTS_DIR / "chat.sqlite"
TITLE_MAX = 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT 'New chat',
    state      TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
"""


def _connect():
    paths.ensure_parent(DB_FILE)
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    return conn


def _title(text):
    t = " ".join(text.split())
    return t if len(t) <= TITLE_MAX else t[:TITLE_MAX - 1].rstrip() + "…"


def list_sessions():
    with closing(_connect()) as c:
        rows = c.execute("SELECT id, title, created_at, updated_at FROM sessions "
                         "ORDER BY updated_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_session(session_id):
    """{id, title, state, messages: [...]} or None. Each message is its stored
    payload (tools/blocks/verification/citations/error) plus role and content."""
    with closing(_connect()) as c:
        s = c.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if s is None:
            return None
        rows = c.execute("SELECT role, content, payload FROM messages WHERE session_id = ? ORDER BY id",
                         (session_id,)).fetchall()
    return {"id": s["id"], "title": s["title"], "state": json.loads(s["state"]),
            "created_at": s["created_at"], "updated_at": s["updated_at"],
            "messages": [{**json.loads(r["payload"]), "role": r["role"], "content": r["content"]} for r in rows]}


def append_message(session_id, role, content, payload=None):
    """Append a message, creating the session on first use (titled from the
    first user message)."""
    now = time.time()
    with closing(_connect()) as c, c:
        if c.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone() is None:
            c.execute("INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                      (session_id, _title(content) if role == "user" else "New chat", now, now))
        c.execute("INSERT INTO messages (session_id, role, content, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                  (session_id, role, content, json.dumps(payload or {}, default=str), now))
        c.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))


def get_state(session_id):
    with closing(_connect()) as c:
        r = c.execute("SELECT state FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return json.loads(r["state"]) if r else {}


def set_state(session_id, state):
    with closing(_connect()) as c, c:
        c.execute("UPDATE sessions SET state = ? WHERE id = ?", (json.dumps(state), session_id))


def rename(session_id, title):
    with closing(_connect()) as c, c:
        cur = c.execute("UPDATE sessions SET title = ? WHERE id = ?", (_title(title) or "New chat", session_id))
        return cur.rowcount > 0


def delete(session_id):
    with closing(_connect()) as c, c:
        return c.execute("DELETE FROM sessions WHERE id = ?", (session_id,)).rowcount > 0
