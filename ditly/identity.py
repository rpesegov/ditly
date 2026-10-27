"""Граница «кто вошёл».

Проекты, роли и ссылки знают только идентификатор пользователя — строку.
Вопрос «кто сейчас вошёл», пароль и сессия живут только в этом модуле.

Разработчики компании позже могут заменить модуль на свою систему учёток.
Остальной код менять не нужно: он вызывает current_user_id() и получает
идентификатор или None. Систему учёток компании сейчас не подключаем.

Таблицы проектов не ссылаются на таблицу users внешним ключом. Идентификатор
может прийти из другого места, когда этот модуль заменят. Сейчас идентификатор
выдаёт местная учётка.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone

from . import db
from .errors import UserError

# Ключ сессии. Пишет и читает его только этот модуль.
SESSION_USER = "user_id"

_LOGIN_RE = re.compile(r"^[\w.\-]{3,64}$", re.UNICODE)
_ITERATIONS = 200_000
_dummy_hash: str | None = None


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            login TEXT NOT NULL,
            login_key TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _dummy() -> str:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password("placeholder-password")
    return _dummy_hash


def validate_login(login: str) -> str:
    cleaned = (login or "").strip()
    if not _LOGIN_RE.fullmatch(cleaned):
        raise UserError(
            "Логин — от 3 до 64 символов: буквы, цифры, точка, дефис или подчёркивание."
        )
    return cleaned


def validate_password(password: str) -> None:
    if not isinstance(password, str) or len(password) < 8:
        raise UserError("Пароль должен быть не короче 8 символов.")
    if len(password) > 200:
        raise UserError("Пароль слишком длинный.")
    if password != password.strip() or any(ord(ch) < 32 for ch in password):
        raise UserError("У пароля не должно быть пробелов по краям и управляющих символов.")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds_raw, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        rounds = int(rounds_raw)
        if rounds < 1 or rounds > 2_000_000:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            rounds,
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (TypeError, ValueError):
        return False


def create_user(login: str, password: str) -> str:
    """Создать учётку сервиса. Сама по себе она не открывает чужие проекты."""
    display = validate_login(login)
    validate_password(password)
    user_id = str(uuid.uuid4())
    try:
        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO users (id, login, login_key, password_hash, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    display,
                    display.casefold(),
                    hash_password(password),
                    _utcnow(),
                ),
            )
    except sqlite3.IntegrityError as exc:
        raise UserError("Учётка с таким логином уже есть.") from exc
    return user_id


def authenticate(login: str, password: str) -> str | None:
    """Проверить логин и пароль. Сессию не открывает."""
    key = (login or "").strip().casefold()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, password_hash FROM users WHERE login_key = ?",
            (key,),
        ).fetchone()
    stored = row["password_hash"] if row is not None else _dummy()
    if not verify_password(password or "", stored):
        return None
    if row is None:
        return None
    return row["id"]


def login(login_name: str, password: str) -> str | None:
    """Проверить пароль и запомнить, кто вошёл. Возвращает идентификатор."""
    user_id = authenticate(login_name, password)
    if user_id is None:
        return None
    from flask import session

    session[SESSION_USER] = user_id
    session.permanent = True
    return user_id


def logout() -> None:
    from flask import session

    session.pop(SESSION_USER, None)


def current_user_id() -> str | None:
    """Идентификатор того, кто вошёл, или None. Единственный вход для остального кода."""
    from flask import has_request_context, session

    if not has_request_context():
        return None
    user_id = session.get(SESSION_USER)
    if isinstance(user_id, str) and user_id:
        return user_id
    return None


def find_user_id(login_name: str) -> str | None:
    key = (login_name or "").strip().casefold()
    if not key:
        return None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE login_key = ?",
            (key,),
        ).fetchone()
    if row is None:
        return None
    return row["id"]


def login_name(user_id: str) -> str | None:
    if not user_id:
        return None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT login FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    return row["login"]
