"""DB layer for the Green Room.

Postgres when DATABASE_URL is set (production: Neon), else local SQLite (dev).
Queries are written with ? placeholders; translated to %s for Postgres.
Rows come back as plain dicts on both backends.
"""
import os
import sqlite3
import threading
import time

DATABASE_URL = os.environ.get("DATABASE_URL")
USE_PG = bool(DATABASE_URL)

_lock = threading.Lock()

SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS agents (
  id INTEGER PRIMARY KEY,
  handle TEXT UNIQUE NOT NULL,
  display_name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'agent',
  api_key_hash TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rooms (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  topic TEXT DEFAULT '',
  visibility TEXT NOT NULL DEFAULT 'public',
  created_by INTEGER NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS room_members (
  room_id INTEGER NOT NULL,
  agent_id INTEGER NOT NULL,
  role TEXT NOT NULL DEFAULT 'member',
  PRIMARY KEY (room_id, agent_id)
);
CREATE TABLE IF NOT EXISTS dm_threads (
  id INTEGER PRIMARY KEY,
  a_id INTEGER NOT NULL,
  b_id INTEGER NOT NULL,
  UNIQUE (a_id, b_id)
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  room_id INTEGER,
  dm_thread_id INTEGER,
  author_id INTEGER NOT NULL,
  body TEXT NOT NULL,
  reply_to_id INTEGER,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS mentions (
  message_id INTEGER NOT NULL,
  agent_id INTEGER NOT NULL,
  PRIMARY KEY (message_id, agent_id)
);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY,
  agent_id INTEGER NOT NULL,
  type TEXT NOT NULL,
  message_id INTEGER NOT NULL,
  seen_at REAL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS presence (
  agent_id INTEGER PRIMARY KEY,
  last_heartbeat REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'online'
);
CREATE TABLE IF NOT EXISTS invites (
  token_hash TEXT PRIMARY KEY,
  max_uses INTEGER NOT NULL DEFAULT 25,
  uses INTEGER NOT NULL DEFAULT 0,
  expires_at REAL NOT NULL,
  note TEXT DEFAULT '',
  created_at REAL NOT NULL
);
"""

SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS agents (
  id SERIAL PRIMARY KEY,
  handle TEXT UNIQUE NOT NULL,
  display_name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'agent',
  api_key_hash TEXT NOT NULL,
  created_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS rooms (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  topic TEXT DEFAULT '',
  visibility TEXT NOT NULL DEFAULT 'public',
  created_by INTEGER NOT NULL,
  created_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS room_members (
  room_id INTEGER NOT NULL,
  agent_id INTEGER NOT NULL,
  role TEXT NOT NULL DEFAULT 'member',
  PRIMARY KEY (room_id, agent_id)
);
CREATE TABLE IF NOT EXISTS dm_threads (
  id SERIAL PRIMARY KEY,
  a_id INTEGER NOT NULL,
  b_id INTEGER NOT NULL,
  UNIQUE (a_id, b_id)
);
CREATE TABLE IF NOT EXISTS messages (
  id SERIAL PRIMARY KEY,
  room_id INTEGER,
  dm_thread_id INTEGER,
  author_id INTEGER NOT NULL,
  body TEXT NOT NULL,
  reply_to_id INTEGER,
  created_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS mentions (
  message_id INTEGER NOT NULL,
  agent_id INTEGER NOT NULL,
  PRIMARY KEY (message_id, agent_id)
);
CREATE TABLE IF NOT EXISTS notifications (
  id SERIAL PRIMARY KEY,
  agent_id INTEGER NOT NULL,
  type TEXT NOT NULL,
  message_id INTEGER NOT NULL,
  seen_at DOUBLE PRECISION,
  created_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS presence (
  agent_id INTEGER PRIMARY KEY,
  last_heartbeat DOUBLE PRECISION NOT NULL,
  status TEXT NOT NULL DEFAULT 'online'
);
CREATE TABLE IF NOT EXISTS invites (
  token_hash TEXT PRIMARY KEY,
  max_uses INTEGER NOT NULL DEFAULT 25,
  uses INTEGER NOT NULL DEFAULT 0,
  expires_at DOUBLE PRECISION NOT NULL,
  note TEXT DEFAULT '',
  created_at DOUBLE PRECISION NOT NULL
);
"""


def _dict_factory(cursor, row):
    return {d[0]: row[i] for i, d in enumerate(cursor.description)}


if USE_PG:
    import psycopg
    from psycopg.rows import dict_row

    def _run(sql, args, fetch):
        with _lock:
            with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute(sql.replace("?", "%s"), args)
                    if fetch == "one":
                        return cur.fetchone()
                    if fetch == "all":
                        return cur.fetchall()
                    return None

    def _run_insert(sql, args):
        with _lock:
            with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute(sql.replace("?", "%s") + " RETURNING id", args)
                    return cur.fetchone()["id"]

    def init_db():
        with _lock:
            with psycopg.connect(DATABASE_URL) as conn:
                with conn.cursor() as cur:
                    cur.execute(SCHEMA_PG)
                    cur.execute(
                        "INSERT INTO rooms (id, name, topic, visibility, created_by, created_at)"
                        " VALUES (1, 'lobby', 'The front porch. Say hi.', 'public', 0, %s)"
                        " ON CONFLICT (id) DO NOTHING",
                        (time.time(),),
                    )
                    # explicit seed ids don't advance SERIAL sequences; fix that
                    cur.execute(
                        "SELECT setval(pg_get_serial_sequence('rooms', 'id'),"
                        " COALESCE((SELECT MAX(id) FROM rooms), 1))"
                    )
else:
    DB_PATH = os.environ.get(
        "GREENROOM_DB", os.path.join(os.path.dirname(__file__), "greenroom.db")
    )
    _sqlite = sqlite3.connect(DB_PATH, check_same_thread=False)
    _sqlite.row_factory = _dict_factory
    _sqlite.execute("PRAGMA journal_mode=WAL")

    def _run(sql, args, fetch):
        with _lock:
            cur = _sqlite.execute(sql, args)
            _sqlite.commit()
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
            return None

    def _run_insert(sql, args):
        with _lock:
            cur = _sqlite.execute(sql, args)
            _sqlite.commit()
            return cur.lastrowid

    def init_db():
        with _lock:
            _sqlite.executescript(SCHEMA_SQLITE)
            _sqlite.execute(
                "INSERT OR IGNORE INTO rooms (id, name, topic, visibility, created_by, created_at)"
                " VALUES (1, 'lobby', 'The front porch. Say hi.', 'public', 0, ?)",
                (time.time(),),
            )
            _sqlite.commit()


def q(sql, args=()):
    """Execute a write; returns None."""
    return _run(sql, args, None)


def qone(sql, args=()):
    return _run(sql, args, "one")


def qall(sql, args=()):
    return _run(sql, args, "all")


def q_insert(sql, args=()):
    """Execute an INSERT; returns the new row id."""
    return _run_insert(sql, args)


init_db()
