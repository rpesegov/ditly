"""SQLite этого процесса. Отдельной базы нет."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from .config import Settings

_settings: Settings | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_roles (
    project_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('view', 'edit', 'manage')),
    PRIMARY KEY (project_id, user_id),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS links (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    code TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL,
    label TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS clicks (
    id TEXT PRIMARY KEY,
    link_id TEXT NOT NULL,
    clicked_at TEXT NOT NULL,
    is_bot INTEGER NOT NULL CHECK (is_bot IN (0, 1)),
    referrer TEXT,
    device TEXT,
    country TEXT,
    visitor_key TEXT,
    FOREIGN KEY (link_id) REFERENCES links(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_roles_user ON project_roles(user_id);
CREATE INDEX IF NOT EXISTS idx_links_project ON links(project_id);
CREATE INDEX IF NOT EXISTS idx_clicks_link ON clicks(link_id, clicked_at);
"""


def configure(settings: Settings) -> None:
    global _settings
    _settings = settings
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    if _settings is None:
        raise RuntimeError("Сервис не настроен.")
    return _settings


def connect_raw() -> sqlite3.Connection:
    settings = get_settings()
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def connect():
    conn = connect_raw()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    from .identity import ensure_schema

    conn = connect_raw()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        ensure_schema(conn)
        conn.commit()
    finally:
        conn.close()
