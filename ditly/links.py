"""Короткие ссылки внутри проекта."""

from __future__ import annotations

import re
import secrets
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

from . import db
from .errors import UserError, required_text

CODE_RE = re.compile(r"^[A-Za-z0-9]{4,16}$")
_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_RESERVED = {
    "login",
    "logout",
    "projects",
    "links",
    "users",
    "static",
    "favicon",
    "robots",
    "health",
    "account",
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def valid_target(url: str) -> str:
    cleaned = (url or "").strip()
    if len(cleaned) > 4000:
        raise UserError("Длинный адрес слишком длинный.")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise UserError("В длинном адресе есть недопустимые символы.")
    parts = urlsplit(cleaned)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise UserError("Длинный адрес должен начинаться с http:// или https://.")
    if parts.username or parts.password:
        raise UserError("Адрес не должен содержать логин и пароль.")
    return cleaned


def _generate_code(conn) -> str:
    for _ in range(10):
        code = "".join(secrets.choice(_ALPHABET) for _ in range(7))
        if code.lower() in _RESERVED:
            continue
        taken = conn.execute("SELECT 1 FROM links WHERE code = ?", (code,)).fetchone()
        if taken is None:
            return code
    raise UserError("Не удалось подобрать код ссылки. Попробуйте ещё раз.")


def create_link(project_id: str, user_id: str, label: str, url: str) -> dict:
    cleaned_label = required_text(label, 200, "Пометка")
    cleaned_url = valid_target(url)
    link_id = str(uuid.uuid4())
    with db.connect() as conn:
        code = _generate_code(conn)
        conn.execute(
            """
            INSERT INTO links (
                id, project_id, code, url, label, enabled, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (link_id, project_id, code, cleaned_url, cleaned_label, user_id, _utcnow()),
        )
    found = get_link(project_id, link_id)
    if found is None:
        raise UserError("Ссылку не удалось сохранить.")
    return found


def list_links(project_id: str) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT l.id, l.project_id, l.code, l.url, l.label, l.enabled, l.created_at,
                   (SELECT COUNT(*) FROM clicks c
                    WHERE c.link_id = l.id AND c.is_bot = 0) AS clicks
            FROM links l
            WHERE l.project_id = ?
            ORDER BY l.created_at DESC, l.label
            """,
            (project_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_link(project_id: str, link_id: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT id, project_id, code, url, label, enabled, created_by, created_at
            FROM links
            WHERE id = ? AND project_id = ?
            """,
            (link_id, project_id),
        ).fetchone()
    if row is None:
        return None
    return dict(row)


def find_enabled_by_code(code: str) -> dict | None:
    if not CODE_RE.fullmatch(code or ""):
        return None
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT id, project_id, code, url, label, enabled
            FROM links
            WHERE code = ? AND enabled = 1
            """,
            (code,),
        ).fetchone()
    if row is None:
        return None
    return dict(row)


def update_link(
    project_id: str,
    link_id: str,
    *,
    label: str,
    url: str,
    enabled: bool,
) -> None:
    if get_link(project_id, link_id) is None:
        raise UserError("Ссылка не найдена.")
    cleaned_label = required_text(label, 200, "Пометка")
    cleaned_url = valid_target(url)
    with db.connect() as conn:
        cur = conn.execute(
            """
            UPDATE links
            SET label = ?, url = ?, enabled = ?
            WHERE id = ? AND project_id = ?
            """,
            (cleaned_label, cleaned_url, 1 if enabled else 0, link_id, project_id),
        )
        if cur.rowcount != 1:
            raise UserError("Ссылка не найдена.")


def delete_link(project_id: str, link_id: str) -> None:
    with db.connect() as conn:
        cur = conn.execute(
            "DELETE FROM links WHERE id = ? AND project_id = ?",
            (link_id, project_id),
        )
        if cur.rowcount != 1:
            raise UserError("Ссылка не найдена.")
