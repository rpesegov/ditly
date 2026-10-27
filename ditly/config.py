"""Настройки процесса. Домен короткой ссылки сюда не зашит."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(SystemExit):
    """Ошибка настройки, из-за которой сервис не стоит запускать."""


@dataclass(frozen=True)
class Settings:
    db_path: Path
    base_url: str
    secret_key: str
    geoip_db: str | None
    trust_proxy: bool
    host: str
    port: int


def normalize_base_url(value: str) -> str:
    """Базовый адрес короткой ссылки: схема, хост и порт, без пути.

    Короткая ссылка собирается как этот адрес плюс /код.
    """
    raw = (value or "").strip()
    if not raw:
        raise ConfigError(
            "Задайте DITLY_BASE_URL — базовый адрес короткой ссылки, "
            "например http://127.0.0.1:8000"
        )
    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ConfigError(
            "DITLY_BASE_URL должен быть адресом http или https, "
            "например http://127.0.0.1:8000"
        )
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigError(
            "DITLY_BASE_URL укажите без логина, параметров и якоря."
        )
    if parts.path not in {"", "/"}:
        raise ConfigError(
            "DITLY_BASE_URL укажите без пути. "
            "Короткая ссылка — это базовый адрес плюс /код."
        )
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{parts.port}" if parts.port else host
    return f"{parts.scheme}://{netloc}"


def _secret(data_dir: Path) -> str:
    from_env = os.environ.get("DITLY_SECRET", "").strip()
    if from_env:
        return from_env
    path = data_dir / "secret.key"
    if path.exists():
        saved = path.read_text(encoding="utf-8").strip()
        if saved:
            return saved
    path.parent.mkdir(parents=True, exist_ok=True)
    generated = secrets.token_hex(32)
    path.write_text(generated, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return generated


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("DITLY_DATA_DIR", "data"))
    db_raw = os.environ.get("DITLY_DB", "").strip()
    db_path = Path(db_raw) if db_raw else data_dir / "ditly.sqlite"
    base_url = normalize_base_url(
        os.environ.get("DITLY_BASE_URL", "http://127.0.0.1:8000")
    )
    geoip = os.environ.get("DITLY_GEOIP_DB", "").strip() or None
    trust = os.environ.get("DITLY_TRUST_PROXY", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    host = os.environ.get("DITLY_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port_raw = os.environ.get("DITLY_PORT", "8000").strip() or "8000"
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise ConfigError("DITLY_PORT должен быть числом.") from exc
    if not 1 <= port <= 65535:
        raise ConfigError("DITLY_PORT должен быть от 1 до 65535.")
    return Settings(
        db_path=db_path,
        base_url=base_url,
        secret_key=_secret(db_path.parent),
        geoip_db=geoip,
        trust_proxy=trust,
        host=host,
        port=port,
    )
