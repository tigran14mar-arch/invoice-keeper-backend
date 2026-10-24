"""
Слой доступа к MySQL.

Здесь и только здесь открываются соединения с базой. Остальные модули бэкенда
работают через функции query_all / query_one / execute / transaction.

Важно про «сегодня»: в базе есть функция fn_today(), которая учитывает демо-сдвиг
(system_settings.override_today). Python обязан использовать ту же дату, поэтому
today() всегда спрашивает её у базы, а не берёт системную дату сервера.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import date
from typing import Any, Iterator, Optional, Sequence

import mysql.connector
from mysql.connector import errorcode
from mysql.connector.pooling import MySQLConnectionPool

from app.config import settings

log = logging.getLogger("invoice_keeper.db")

_pool: Optional[MySQLConnectionPool] = None

REQUIRED_OBJECTS = (
    "users",
    "user_sessions",
    "recurring_payments",
    "payment_occurrences",
    "system_settings",
    "audit_logs",
    "budgets",
    "v_payment_occurrences_detailed",
    "v_payments_summary_by_currency",
    "telegram_messages",
)


class DatabaseError(RuntimeError):
    """Ошибка работы с базой, понятная человеку."""


def init_pool() -> None:
    """Создаёт пул соединений. Вызывается один раз при старте приложения."""
    global _pool
    if _pool is not None:
        return
    try:
        _pool = MySQLConnectionPool(
            pool_name="invoice_keeper_pool",
            pool_size=max(2, settings.db_pool_size),
            pool_reset_session=True,
            host=settings.db_host,
            port=settings.db_port,
            user=settings.db_user,
            password=settings.db_password,
            database=settings.db_name,
            charset="utf8mb4",
            collation="utf8mb4_unicode_ci",
            time_zone=settings.mysql_time_zone,
            autocommit=True,
            connection_timeout=10,
            use_pure=True,
            **({"unix_socket": settings.db_unix_socket} if settings.db_unix_socket else {}),
        )
    except mysql.connector.Error as exc:
        raise DatabaseError(_friendly_error(exc)) from exc
    log.info(
        "Пул соединений MySQL создан: %s@%s:%s/%s (часовой пояс %s)",
        settings.db_user, settings.db_host, settings.db_port, settings.db_name, settings.mysql_time_zone,
    )


def close_pool() -> None:
    global _pool
    _pool = None


def _friendly_error(exc: mysql.connector.Error) -> str:
    if exc.errno == errorcode.ER_ACCESS_DENIED_ERROR:
        return "MySQL отказал в доступе: проверьте DB_USER и DB_PASSWORD в .env"
    if exc.errno == errorcode.ER_BAD_DB_ERROR:
        return (f"База данных '{settings.db_name}' не найдена. "
                f"Импортируйте db/invoice_keeper_db.sql и проверьте DB_NAME в .env")
    if exc.errno == 2003:
        return (f"MySQL недоступен на {settings.db_host}:{settings.db_port}. "
                f"Проверьте, что сервер запущен, и параметры DB_HOST / DB_PORT в .env")
    return f"Ошибка MySQL: {exc}"


@contextmanager
def get_connection() -> Iterator[Any]:
    """Соединение из пула. Возвращается в пул автоматически."""
    if _pool is None:
        init_pool()
    try:
        conn = _pool.get_connection()  # type: ignore[union-attr]
    except mysql.connector.Error as exc:
        raise DatabaseError(_friendly_error(exc)) from exc
    try:
        # Соединения переиспользуются: обнуляем возможный ручной сдвиг даты,
        # чтобы fn_today() читал настройку из system_settings, а не чужую @app_today.
        with conn.cursor() as cur:
            cur.execute("SET @app_today = NULL")
        yield conn
    finally:
        try:
            conn.close()  # вернуть в пул
        except mysql.connector.Error:
            pass


@contextmanager
def transaction() -> Iterator[Any]:
    """Соединение с явной транзакцией: commit при успехе, rollback при исключении."""
    with get_connection() as conn:
        conn.start_transaction()
        try:
            yield conn
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except mysql.connector.Error:
                pass
            raise


def _run(conn: Any, sql: str, params: Sequence[Any] | None, dictionary: bool) -> tuple[list[dict], int, Optional[int]]:
    with conn.cursor(dictionary=dictionary) as cur:
        cur.execute(sql, tuple(params or ()))
        rows = cur.fetchall() if cur.with_rows else []
        return rows, cur.rowcount, cur.lastrowid


def query_all(sql: str, params: Sequence[Any] | None = None, *, conn: Any = None) -> list[dict]:
    """SELECT: список словарей."""
    if conn is not None:
        return _run(conn, sql, params, True)[0]
    with get_connection() as own:
        return _run(own, sql, params, True)[0]


def query_one(sql: str, params: Sequence[Any] | None = None, *, conn: Any = None) -> Optional[dict]:
    """SELECT: первая строка или None."""
    rows = query_all(sql, params, conn=conn)
    return rows[0] if rows else None


def query_value(sql: str, params: Sequence[Any] | None = None, *, conn: Any = None) -> Any:
    """SELECT одного значения (например, SELECT fn_today())."""
    row = query_one(sql, params, conn=conn)
    return next(iter(row.values())) if row else None


def execute(sql: str, params: Sequence[Any] | None = None, *, conn: Any = None) -> tuple[int, Optional[int]]:
    """INSERT / UPDATE / DELETE. Возвращает (количество затронутых строк, lastrowid)."""
    if conn is not None:
        _, rowcount, lastrowid = _run(conn, sql, params, False)
        return rowcount, lastrowid
    with get_connection() as own:
        _, rowcount, lastrowid = _run(own, sql, params, False)
        return rowcount, lastrowid


def today(conn: Any = None) -> date:
    """«Сегодня» глазами базы: @app_today -> system_settings.override_today -> CURDATE()."""
    value = query_value("SELECT fn_today() AS today", conn=conn)
    if isinstance(value, date):
        return value
    raise DatabaseError("Функция fn_today() недоступна. Импортирован ли db/invoice_keeper_db.sql?")


def check_schema() -> None:
    """Проверяет, что база импортирована: все таблицы, представления и функции на месте."""
    rows = query_all(
        "SELECT TABLE_NAME AS name FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s",
        (settings.db_name,),
    )
    existing = {row["name"] for row in rows}
    missing = [name for name in REQUIRED_OBJECTS if name not in existing]
    if missing:
        raise DatabaseError(
            "В базе не хватает объектов: " + ", ".join(missing) +
            ". Для новой базы импортируйте db/invoice_keeper_db.sql, затем "
            "db/migration_group_bot.sql. Для существующей базы примените только "
            "недостающую миграцию (см. README); основной дамп удаляет данные."
        )
    functions = query_all(
        "SELECT ROUTINE_NAME AS name FROM information_schema.ROUTINES "
        "WHERE ROUTINE_SCHEMA = %s AND ROUTINE_TYPE = 'FUNCTION'",
        (settings.db_name,),
    )
    have = {row["name"] for row in functions}
    missing_fn = [name for name in ("fn_today", "fn_calc_monthly_due_date") if name not in have]
    if missing_fn:
        raise DatabaseError(
            "В базе не хватает функций: " + ", ".join(missing_fn) +
            ". Импортируйте db/invoice_keeper_db.sql целиком (вместе с DELIMITER-блоками)."
        )
