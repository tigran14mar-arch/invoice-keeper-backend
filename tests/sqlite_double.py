"""
Двойник базы на SQLite для автономных тестов бизнес-логики.

Зачем: проверить правила кейса (якорный день, просрочка, напоминания, права)
не поднимая MySQL. Схема, представление v_payment_occurrences_detailed и триггеры
повторяют db/invoice_keeper_db.sql один в один по смыслу; отличия только там,
где синтаксис SQLite обязывает:

 * BEFORE-триггеры SQLite не умеют менять NEW, поэтому те же правила выражены
   через AFTER-триггеры (рекурсия у SQLite по умолчанию выключена);
 * MySQL-функции fn_today(), CURDATE(), NOW(), DATEDIFF() зарегистрированы как
   функции Python;
 * DATE_ADD(x, INTERVAL n DAY) записан как DATE_ADD_DAYS(x, n).

Это НЕ замена проверке на настоящей MySQL (для неё есть scripts/smoke_api.py),
а быстрый способ убедиться, что логика приложения верна.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import types
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Iterator, Optional, Sequence

# --- значения .env для тестовой среды (до импорта app.config) ---
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin12345"
os.environ["ADMIN_FULL_NAME"] = "Администратор"
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

STATE: dict[str, Any] = {"real_today": date(2026, 10, 24), "override": None, "clock": 0}
_conn: Optional[sqlite3.Connection] = None


# =============================================================================
# Функции, которые в MySQL встроены или объявлены в дампе
# =============================================================================

def _fn_today() -> str:
    override = STATE["override"]
    return (override or STATE["real_today"]).isoformat()


def _curdate() -> str:
    return STATE["real_today"].isoformat()


def _now() -> str:
    STATE["clock"] += 1  # монотонность, чтобы сортировка по времени была предсказуемой
    base = datetime.combine(STATE["real_today"], datetime.min.time()) + timedelta(seconds=STATE["clock"])
    return base.strftime("%Y-%m-%d %H:%M:%S")


def _datediff(left: Optional[str], right: Optional[str]) -> Optional[int]:
    if left is None or right is None:
        return None
    return (date.fromisoformat(left[:10]) - date.fromisoformat(right[:10])).days


def _date_add_days(value: Optional[str], days: Optional[int]) -> Optional[str]:
    if value is None or days is None:
        return None
    return (date.fromisoformat(value[:10]) + timedelta(days=int(days))).isoformat()


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE system_settings (
    id INTEGER PRIMARY KEY DEFAULT 1,
    timezone TEXT NOT NULL DEFAULT 'Europe/Moscow',
    override_today DATE DEFAULT NULL,
    updated_at DATETIME NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO system_settings (id, timezone, override_today) VALUES (1, 'Europe/Moscow', NULL);

CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    full_name TEXT DEFAULT NULL,
    role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin','user')),
    is_env_admin INTEGER NOT NULL DEFAULT 0,
    telegram_user_id INTEGER DEFAULT NULL UNIQUE,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL DEFAULT (datetime('now')),
    updated_at DATETIME NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE user_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_token TEXT NOT NULL UNIQUE,
    user_id INTEGER DEFAULT NULL REFERENCES users(id) ON DELETE CASCADE,
    username TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin','user')),
    expires_at DATETIME NOT NULL,
    is_revoked INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE recurring_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    amount DECIMAL(15,2) NOT NULL CHECK (amount > 0),
    currency TEXT NOT NULL CHECK (currency IN ('RUB','USD','EUR')),
    periodicity TEXT NOT NULL DEFAULT 'monthly' CHECK (periodicity IN ('monthly')),
    start_date DATE NOT NULL,
    anchor_day INTEGER NOT NULL CHECK (anchor_day BETWEEN 1 AND 31),
    remind_days_before INTEGER NOT NULL DEFAULT 3 CHECK (remind_days_before >= 3),
    is_deleted INTEGER NOT NULL DEFAULT 0,
    deleted_at DATETIME DEFAULT NULL,
    created_by TEXT NOT NULL DEFAULT 'admin',
    created_at DATETIME NOT NULL DEFAULT (datetime('now')),
    updated_at DATETIME NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE payment_occurrences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recurring_payment_id INTEGER NOT NULL REFERENCES recurring_payments(id),
    amount DECIMAL(15,2) NOT NULL CHECK (amount > 0),
    currency TEXT NOT NULL CHECK (currency IN ('RUB','USD','EUR')),
    due_date DATE NOT NULL,
    is_paid INTEGER NOT NULL DEFAULT 0,
    actual_payment_date DATE DEFAULT NULL,
    paid_by_username TEXT DEFAULT NULL,
    reminded_at DATETIME DEFAULT NULL,
    telegram_message_id INTEGER DEFAULT NULL,
    receipt_url TEXT DEFAULT NULL,
    notes TEXT DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT (datetime('now')),
    updated_at DATETIME NOT NULL DEFAULT (datetime('now')),
    UNIQUE (recurring_payment_id, due_date),
    CHECK ((is_paid = 0 AND actual_payment_date IS NULL) OR (is_paid = 1 AND actual_payment_date IS NOT NULL))
);

CREATE TABLE budgets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_type TEXT NOT NULL CHECK (period_type IN ('monthly','yearly')),
    period_year INTEGER NOT NULL,
    period_month INTEGER NOT NULL DEFAULT 0,
    currency TEXT NOT NULL CHECK (currency IN ('RUB','USD','EUR')),
    planned_limit DECIMAL(15,2) NOT NULL CHECK (planned_limit >= 0),
    created_at DATETIME NOT NULL DEFAULT (datetime('now')),
    updated_at DATETIME NOT NULL DEFAULT (datetime('now')),
    UNIQUE (period_type, period_year, period_month, currency)
);

CREATE TABLE audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_username TEXT NOT NULL,
    actor_role TEXT NOT NULL CHECK (actor_role IN ('admin','user')),
    action_type TEXT NOT NULL,
    entity_type TEXT DEFAULT NULL,
    entity_id INTEGER DEFAULT NULL,
    details TEXT DEFAULT NULL,
    ip_address TEXT DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT (datetime('now'))
);

-- Триггеры: те же правила, что BEFORE-триггеры в MySQL
CREATE TRIGGER trg_po_after_insert AFTER INSERT ON payment_occurrences
BEGIN
    UPDATE payment_occurrences SET actual_payment_date = NULL
      WHERE id = NEW.id AND is_paid = 0 AND actual_payment_date IS NOT NULL;
    UPDATE payment_occurrences SET actual_payment_date = fn_today()
      WHERE id = NEW.id AND is_paid = 1 AND actual_payment_date IS NULL;
END;

CREATE TRIGGER trg_po_after_update AFTER UPDATE ON payment_occurrences
BEGIN
    UPDATE payment_occurrences SET due_date = OLD.due_date
      WHERE id = NEW.id AND due_date <> OLD.due_date;
    UPDATE payment_occurrences SET actual_payment_date = NULL
      WHERE id = NEW.id AND is_paid = 0 AND actual_payment_date IS NOT NULL;
    UPDATE payment_occurrences SET actual_payment_date = fn_today()
      WHERE id = NEW.id AND is_paid = 1 AND actual_payment_date IS NULL;
    UPDATE payment_occurrences SET updated_at = NOW() WHERE id = NEW.id;
END;

CREATE VIEW v_payment_occurrences_detailed AS
SELECT
    po.id AS id,
    po.recurring_payment_id AS recurring_payment_id,
    rp.name AS name,
    po.amount AS amount,
    po.currency AS currency,
    rp.periodicity AS periodicity,
    rp.anchor_day AS anchor_day,
    rp.is_deleted AS is_recurring_deleted,
    po.due_date AS due_date,
    po.is_paid AS is_paid,
    CASE WHEN po.is_paid = 1 THEN 'paid' ELSE 'unpaid' END AS status,
    po.actual_payment_date AS actual_payment_date,
    CASE
        WHEN po.is_paid = 0 AND fn_today() > po.due_date THEN 1
        WHEN po.is_paid = 1 AND po.actual_payment_date > po.due_date THEN 1
        ELSE 0
    END AS is_overdue,
    CASE
        WHEN po.is_paid = 0 AND fn_today() > po.due_date THEN DATEDIFF(fn_today(), po.due_date)
        WHEN po.is_paid = 1 AND po.actual_payment_date > po.due_date THEN DATEDIFF(po.actual_payment_date, po.due_date)
        ELSE 0
    END AS overdue_days,
    CASE
        WHEN rp.is_deleted = 0 AND po.is_paid = 0 AND po.reminded_at IS NULL
             AND po.due_date <= DATE_ADD_DAYS(fn_today(), rp.remind_days_before)
        THEN 1 ELSE 0
    END AS needs_telegram_reminder,
    po.paid_by_username AS paid_by_username,
    po.reminded_at AS reminded_at,
    po.telegram_message_id AS telegram_message_id,
    po.receipt_url AS receipt_url,
    po.notes AS notes,
    po.created_at AS created_at,
    po.updated_at AS updated_at
FROM payment_occurrences po
JOIN recurring_payments rp ON rp.id = po.recurring_payment_id;

CREATE VIEW v_payments_summary_by_currency AS
SELECT
    c.currency AS currency,
    COUNT(v.id) AS total_periods_count,
    COALESCE(SUM(v.amount), 0.00) AS total_amount,
    COALESCE(SUM(CASE WHEN v.is_paid = 1 THEN v.amount ELSE 0.00 END), 0.00) AS paid_amount,
    COALESCE(SUM(CASE WHEN v.is_paid = 0 THEN v.amount ELSE 0.00 END), 0.00) AS unpaid_amount,
    COALESCE(SUM(CASE WHEN v.is_overdue = 1 AND v.is_paid = 0 THEN v.amount ELSE 0.00 END), 0.00)
        AS active_overdue_amount,
    COALESCE(SUM(CASE WHEN v.is_overdue = 1 AND v.is_paid = 1 THEN v.amount ELSE 0.00 END), 0.00)
        AS paid_with_overdue_amount,
    COUNT(CASE WHEN v.is_overdue = 1 AND v.is_paid = 0 THEN 1 END) AS active_overdue_count,
    COUNT(CASE WHEN v.is_paid = 0 THEN 1 END) AS unpaid_count,
    COUNT(CASE WHEN v.is_paid = 1 THEN 1 END) AS paid_count
FROM (SELECT 'RUB' AS currency UNION ALL SELECT 'USD' UNION ALL SELECT 'EUR') c
LEFT JOIN v_payment_occurrences_detailed v ON v.currency = c.currency
GROUP BY c.currency;
"""


# =============================================================================
# Перевод диалекта MySQL -> SQLite
# =============================================================================

def translate(sql: str) -> str:
    sql = sql.replace("%s", "?")
    sql = re.sub(r"\bINSERT\s+IGNORE\s+INTO\b", "INSERT OR IGNORE INTO", sql, flags=re.I)
    sql = re.sub(r"\s+FOR\s+UPDATE\b", "", sql, flags=re.I)
    sql = re.sub(
        r"ON\s+DUPLICATE\s+KEY\s+UPDATE\s+planned_limit\s*=\s*VALUES\(planned_limit\)",
        "ON CONFLICT(period_type, period_year, period_month, currency) "
        "DO UPDATE SET planned_limit = excluded.planned_limit",
        sql, flags=re.I,
    )
    sql = re.sub(r"NOW\(\)\s*-\s*INTERVAL\s+(\d+)\s+DAY", r"DATE_ADD_DAYS(NOW(), -\1)", sql, flags=re.I)
    sql = sql.replace("TRUE", "1").replace("FALSE", "0")
    return sql


def _convert(value: Any) -> Any:
    if isinstance(value, str):
        if ISO_DATE.match(value):
            return date.fromisoformat(value)
        if ISO_DATETIME.match(value):
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    if isinstance(value, float):
        return Decimal(str(value)).quantize(Decimal("0.01"))
    return value


def _row_to_dict(cursor: sqlite3.Cursor, row: tuple) -> dict:
    return {column[0]: _convert(value) for column, value in zip(cursor.description, row)}


# =============================================================================
# Подмена модуля app.database
# =============================================================================

class _FakeError(RuntimeError):
    pass


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        _conn.close()
    sqlite3.register_adapter(date, lambda value: value.isoformat())
    sqlite3.register_adapter(datetime, lambda value: value.strftime("%Y-%m-%d %H:%M:%S"))
    sqlite3.register_adapter(Decimal, str)
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = _row_to_dict
    conn.create_function("fn_today", 0, _fn_today)
    conn.create_function("CURDATE", 0, _curdate)
    conn.create_function("NOW", 0, _now)
    conn.create_function("DATEDIFF", 2, _datediff)
    conn.create_function("DATE_ADD_DAYS", 2, _date_add_days)
    conn.executescript(SCHEMA)
    _conn = conn
    return conn


def reset(real_today: date = date(2026, 10, 24)) -> None:
    """Пересоздаёт базу перед каждым тестом."""
    STATE["real_today"] = real_today
    STATE["override"] = None
    STATE["clock"] = 0
    connect()


def _refresh_override() -> None:
    row = _conn.execute("SELECT override_today FROM system_settings WHERE id = 1").fetchone()
    value = row["override_today"] if row else None
    STATE["override"] = value if isinstance(value, date) else (date.fromisoformat(value) if value else None)


def _execute(sql: str, params: Sequence[Any] | None):
    cursor = _conn.execute(translate(sql), tuple(params or ()))
    if re.search(r"system_settings", sql, re.I) and re.match(r"\s*UPDATE", sql, re.I):
        _refresh_override()
    return cursor


def query_all(sql: str, params: Sequence[Any] | None = None, *, conn: Any = None) -> list[dict]:
    return _execute(sql, params).fetchall()


def query_one(sql: str, params: Sequence[Any] | None = None, *, conn: Any = None) -> Optional[dict]:
    rows = query_all(sql, params)
    return rows[0] if rows else None


def query_value(sql: str, params: Sequence[Any] | None = None, *, conn: Any = None) -> Any:
    row = query_one(sql, params)
    return next(iter(row.values())) if row else None


def execute(sql: str, params: Sequence[Any] | None = None, *, conn: Any = None) -> tuple[int, Optional[int]]:
    cursor = _execute(sql, params)
    return cursor.rowcount, cursor.lastrowid


@contextmanager
def get_connection() -> Iterator[Any]:
    yield _conn


@contextmanager
def transaction() -> Iterator[Any]:
    in_transaction = _conn.in_transaction
    if not in_transaction:
        _conn.execute("BEGIN")
    try:
        yield _conn
        if not in_transaction:
            _conn.execute("COMMIT")
    except Exception:
        if not in_transaction:
            _conn.execute("ROLLBACK")
        raise


def today(conn: Any = None) -> date:
    return STATE["override"] or STATE["real_today"]


def check_schema() -> None:
    return None


def init_pool() -> None:
    return None


def close_pool() -> None:
    return None


def install() -> None:
    """Ставит двойника вместо настоящих модулей. Вызывать ДО импорта app.services."""
    module = types.ModuleType("app.database")
    module.DatabaseError = _FakeError
    for name in (
        "query_all", "query_one", "query_value", "execute", "get_connection",
        "transaction", "today", "check_schema", "init_pool", "close_pool",
    ):
        setattr(module, name, globals()[name])
    sys.modules["app.database"] = module

    # bcrypt: в тестах достаточно детерминированной подписи, без медленного хеширования
    if "bcrypt" not in sys.modules:
        import hashlib

        fake_bcrypt = types.ModuleType("bcrypt")
        fake_bcrypt.gensalt = lambda rounds=12: b"$2b$12$testsalt"
        fake_bcrypt.hashpw = lambda password, salt: b"hash:" + hashlib.sha256(salt + password).hexdigest().encode()
        fake_bcrypt.checkpw = lambda password, hashed: fake_bcrypt.hashpw(password, b"$2b$12$testsalt") == hashed
        sys.modules["bcrypt"] = fake_bcrypt

    # fastapi: подменяется отдельным модулем (tests/fastapi_stub.py), если пакет не установлен
    from tests import fastapi_stub

    fastapi_stub.install()
