"""Проекты и роли. Здесь нет паролей и сессий, только идентификатор пользователя."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from . import db
from .errors import UserError, required_text

ROLES = ("view", "edit", "manage")

ROLE_LABELS = {
    "view": "Просмотр",
    "edit": "Редактирование",
    "manage": "Управление доступом",
}


def enough(have: str | None, need: str) -> bool:
    if have not in ROLES:
        return False
    return ROLES.index(have) >= ROLES.index(need)


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def create_project(user_id: str, name: str) -> str:
    cleaned = required_text(name, 120, "Название")
    project_id = str(uuid.uuid4())
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO projects (id, name, created_by, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (project_id, cleaned, user_id, _utcnow()),
        )
        conn.execute(
            """
            INSERT INTO project_roles (project_id, user_id, role)
            VALUES (?, ?, 'manage')
            """,
            (project_id, user_id),
        )
    return project_id


def list_projects(user_id: str) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT p.id, p.name, p.created_at, r.role,
                   (SELECT COUNT(*) FROM links l WHERE l.project_id = p.id) AS link_count
            FROM projects p
            JOIN project_roles r ON r.project_id = p.id
            WHERE r.user_id = ?
            ORDER BY p.created_at DESC, p.name
            """,
            (user_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_project(project_id: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, name, created_by, created_at FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
    if row is None:
        return None
    return dict(row)


def get_role(user_id: str | None, project_id: str) -> str | None:
    if not user_id:
        return None
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT role FROM project_roles
            WHERE project_id = ? AND user_id = ?
            """,
            (project_id, user_id),
        ).fetchone()
    if row is None:
        return None
    return row["role"]


def rename_project(project_id: str, name: str) -> None:
    cleaned = required_text(name, 120, "Название")
    with db.connect() as conn:
        conn.execute(
            "UPDATE projects SET name = ? WHERE id = ?",
            (cleaned, project_id),
        )


def delete_project(project_id: str) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


def list_members(project_id: str) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT user_id, role FROM project_roles
            WHERE project_id = ?
            """,
            (project_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _other_managers(conn, project_id: str, user_id: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM project_roles
        WHERE project_id = ? AND role = 'manage' AND user_id != ?
        """,
        (project_id, user_id),
    ).fetchone()
    return int(row["n"])


def set_member(project_id: str, user_id: str, role: str) -> None:
    if role not in ROLES:
        raise UserError("Выберите роль: просмотр, редактирование или управление доступом.")
    with db.connect() as conn:
        current = conn.execute(
            """
            SELECT role FROM project_roles
            WHERE project_id = ? AND user_id = ?
            """,
            (project_id, user_id),
        ).fetchone()
        if current is not None and current["role"] == "manage" and role != "manage":
            if _other_managers(conn, project_id, user_id) == 0:
                raise UserError(
                    "В проекте должен остаться хотя бы один человек с управлением доступом."
                )
        conn.execute(
            """
            INSERT INTO project_roles (project_id, user_id, role)
            VALUES (?, ?, ?)
            ON CONFLICT(project_id, user_id) DO UPDATE SET role = excluded.role
            """,
            (project_id, user_id, role),
        )


def remove_member(project_id: str, user_id: str) -> None:
    with db.connect() as conn:
        current = conn.execute(
            """
            SELECT role FROM project_roles
            WHERE project_id = ? AND user_id = ?
            """,
            (project_id, user_id),
        ).fetchone()
        if current is None:
            raise UserError("У этого человека нет доступа к проекту.")
        if current["role"] == "manage" and _other_managers(conn, project_id, user_id) == 0:
            raise UserError(
                "В проекте должен остаться хотя бы один человек с управлением доступом."
            )
        conn.execute(
            "DELETE FROM project_roles WHERE project_id = ? AND user_id = ?",
            (project_id, user_id),
        )
