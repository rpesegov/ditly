"""Запись перехода и сводка. Сырой IP и User-Agent не хранятся."""

from __future__ import annotations

import ipaddress
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import db

MOSCOW = ZoneInfo("Europe/Moscow")

DEVICE_LABELS = {
    "phone": "Телефон",
    "computer": "Компьютер",
    "tablet": "Планшет",
    "unknown": "Неизвестно",
}

# Признак автоматического захода ставим по User-Agent. Он может ошибаться.
_BOT_MARKERS = (
    "googlebot",
    "bingbot",
    "yandexbot",
    "baiduspider",
    "duckduckbot",
    "slurp",
    "facebookexternalhit",
    "facebot",
    "twitterbot",
    "linkedinbot",
    "telegrambot",
    "slackbot",
    "discordbot",
    "whatsapp",
    "vkshare",
    "applebot",
    "ahrefsbot",
    "semrushbot",
    "dotbot",
    "petalbot",
    "bytespider",
    "gptbot",
    "claudebot",
    "anthropic-ai",
    "ccbot",
    "pingdom",
    "uptimerobot",
    "headlesschrome",
    "phantomjs",
    "scrapy",
    "python-requests",
    "go-http-client",
    "curl/",
    "wget/",
    "httpclient",
    "libwww",
    "spider",
    "crawler",
    "mediapartners-google",
    "embedly",
    "redditbot",
    "pinterestbot",
    "ia_archiver",
    "mail.ru_bot",
    "mj12bot",
    "seznambot",
    "bingpreview",
    "previewbot",
)
_BOT_TOKEN = re.compile(r"(?:^|[^a-z])bot(?:[^a-z]|$)")


@dataclass
class Slice:
    label: str
    count: int
    width: int


@dataclass
class Stats:
    total: int
    bot_count: int
    include_bots: bool
    days: list[Slice]
    referrers: list[Slice]
    devices: list[Slice]
    countries: list[Slice]
    visitors: int


def classify_user_agent(user_agent: str | None) -> tuple[bool, str | None]:
    """Вернуть (автоматический заход, устройство).

    Устройство — phone, computer, tablet, unknown. Если User-Agent пустой,
    устройство неизвестно и в базу пишется пустое поле.
    """
    if user_agent is None or not user_agent.strip():
        return False, None
    low = user_agent.strip().lower()
    return _is_bot(low), _device(low)


def _is_bot(low: str) -> bool:
    if any(marker in low for marker in _BOT_MARKERS):
        return True
    return _BOT_TOKEN.search(low) is not None


def _device(low: str) -> str:
    if any(marker in low for marker in ("ipad", "tablet", "kindle", "silk", "playbook")):
        return "tablet"
    if any(marker in low for marker in ("iphone", "ipod", "windows phone", "mobile", "phone")):
        return "phone"
    if "android" in low:
        return "tablet"
    if any(
        marker in low
        for marker in ("windows nt", "macintosh", "mac os x", "cros", "linux", "x11")
    ):
        return "computer"
    return "unknown"


def clean_referrer(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned or any(ord(ch) < 32 for ch in cleaned):
        return None
    return cleaned[:2000]


def country_from_ip(ip: str | None, geoip_db: str | None) -> str | None:
    """Страна по IP, если есть локальная база. Сам IP наружу не отдаём и не храним."""
    if not ip or not geoip_db:
        return None
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if (
        address.is_private
        or address.is_loopback
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
        or address.is_link_local
    ):
        return None
    try:
        import maxminddb
    except ImportError:
        return None
    try:
        with maxminddb.open_database(geoip_db) as reader:
            record = reader.get(ip)
    except (OSError, ValueError, RuntimeError):
        return None
    if not isinstance(record, dict):
        return None
    country = record.get("country") or record.get("registered_country") or {}
    if not isinstance(country, dict):
        return None
    code = country.get("iso_code")
    if isinstance(code, str) and re.fullmatch(r"[A-Z]{2}", code):
        return code
    return None


def record_click(
    link_id: str,
    *,
    is_bot: bool,
    referrer: str | None,
    device: str | None,
    country: str | None,
    visitor_key: str | None,
    clicked_at: str | None = None,
) -> None:
    moment = clicked_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO clicks (
                id, link_id, clicked_at, is_bot, referrer, device, country, visitor_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                link_id,
                moment,
                1 if is_bot else 0,
                referrer,
                device,
                country,
                visitor_key,
            ),
        )


def moscow_day(clicked_at: str) -> str:
    moment = datetime.fromisoformat(clicked_at)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(MOSCOW).date().isoformat()


def stats_for(link_id: str, *, include_bots: bool) -> Stats:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT clicked_at, is_bot, referrer, device, country, visitor_key
            FROM clicks
            WHERE link_id = ?
            """,
            (link_id,),
        ).fetchall()
    days: Counter[str] = Counter()
    referrers: Counter[str] = Counter()
    devices: Counter[str] = Counter()
    countries: Counter[str] = Counter()
    visitors: set[str] = set()
    total = 0
    bot_count = 0
    for row in rows:
        if row["is_bot"]:
            bot_count += 1
            if not include_bots:
                continue
        total += 1
        days[moscow_day(row["clicked_at"])] += 1
        if row["referrer"]:
            referrers[row["referrer"]] += 1
        if row["device"]:
            devices[row["device"]] += 1
        if row["country"]:
            countries[row["country"]] += 1
        if row["visitor_key"]:
            visitors.add(row["visitor_key"])
    return Stats(
        total=total,
        bot_count=bot_count,
        include_bots=include_bots,
        days=_day_slices(days),
        referrers=_slices(referrers),
        devices=_slices(devices, DEVICE_LABELS),
        countries=_slices(countries),
        visitors=len(visitors),
    )


def _slices(counter: Counter[str], labels: dict[str, str] | None = None) -> list[Slice]:
    if not counter:
        return []
    ranked = counter.most_common(20)
    peak = max(count for _, count in ranked) or 1
    rows = []
    for key, count in ranked:
        label = labels.get(key, key) if labels else key
        rows.append(Slice(label=label, count=count, width=_width(count, peak)))
    return rows


def _day_slices(counter: Counter[str]) -> list[Slice]:
    if not counter:
        return []
    peak = max(counter.values()) or 1
    rows = []
    for iso in sorted(counter):
        year, month, day = iso.split("-")
        rows.append(
            Slice(
                label=f"{day}.{month}.{year}",
                count=counter[iso],
                width=_width(counter[iso], peak),
            )
        )
    return rows


def _width(count: int, peak: int) -> int:
    if count <= 0:
        return 0
    return max(4, round(count * 100 / peak))
