"""
Конфигурация приложения (читается из .env).

Единственный источник настроек для всех остальных модулей бэкенда:
подключение к MySQL, учётная запись администратора, сессии, Telegram-бот.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _get_int(name: str, default: int) -> int:
    raw = _get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Переменная окружения {name} должна быть целым числом, получено: {raw!r}") from exc


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "да"}


def _get_list(name: str, default: str = "") -> list[str]:
    raw = _get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    # --- MySQL ---
    db_host: str = field(default_factory=lambda: _get("DB_HOST", "127.0.0.1"))
    db_port: int = field(default_factory=lambda: _get_int("DB_PORT", 3306))
    db_user: str = field(default_factory=lambda: _get("DB_USER", "root"))
    db_password: str = field(default_factory=lambda: _get("DB_PASSWORD", ""))
    db_name: str = field(default_factory=lambda: _get("DB_NAME", "invoice_keeper"))
    db_pool_size: int = field(default_factory=lambda: _get_int("DB_POOL_SIZE", 8))

    # --- Администратор задаётся ТОЛЬКО здесь (Слайд 6, 9): через API его не изменить ---
    admin_username: str = field(default_factory=lambda: _get("ADMIN_USERNAME", "admin"))
    admin_password: str = field(default_factory=lambda: _get("ADMIN_PASSWORD", "admin"))
    admin_full_name: str = field(default_factory=lambda: _get("ADMIN_FULL_NAME", "Администратор"))

    # --- Сессии ---
    session_ttl_hours: int = field(default_factory=lambda: _get_int("SESSION_TTL_HOURS", 24))
    cookie_name: str = field(default_factory=lambda: _get("SESSION_COOKIE_NAME", "invoice_keeper_session"))
    cookie_secure: bool = field(default_factory=lambda: _get_bool("SESSION_COOKIE_SECURE", False))

    # --- Время (Слайд 4: единый часовой пояс) ---
    app_timezone: str = field(default_factory=lambda: _get("APP_TIMEZONE", "Europe/Moscow"))

    # --- Telegram (Слайд 7, 8) ---
    telegram_bot_token: str = field(default_factory=lambda: _get("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str = field(default_factory=lambda: _get("TELEGRAM_CHAT_ID"))
    telegram_paid_button: bool = field(default_factory=lambda: _get_bool("TELEGRAM_PAID_BUTTON", True))

    # --- Фоновый планировщик ---
    scheduler_interval_seconds: int = field(default_factory=lambda: _get_int("SCHEDULER_INTERVAL_SECONDS", 60))
    scheduler_enabled: bool = field(default_factory=lambda: _get_bool("SCHEDULER_ENABLED", True))
    default_remind_days_before: int = field(default_factory=lambda: _get_int("DEFAULT_REMIND_DAYS_BEFORE", 3))

    # --- HTTP ---
    cors_origins: list[str] = field(default_factory=lambda: _get_list("CORS_ORIGINS", "*"))

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def tzinfo(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.app_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("Europe/Moscow")

    @property
    def mysql_time_zone(self) -> str:
        """Смещение вида '+03:00' — ставится каждому соединению, чтобы CURDATE()
        в MySQL совпадал с «сегодня» приложения (Слайд 4: единый часовой пояс)."""
        offset = datetime.now(self.tzinfo).utcoffset()
        total_minutes = int(offset.total_seconds() // 60) if offset else 0
        sign = "+" if total_minutes >= 0 else "-"
        total_minutes = abs(total_minutes)
        return f"{sign}{total_minutes // 60:02d}:{total_minutes % 60:02d}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
