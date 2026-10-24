"""
Бизнес-логика приложения — то, что API вызывает после проверки прав.

Содержит все операции чтения и записи по одной предметной области в одном месте:
 * регулярные платежи (создание, список, прекращение с сохранением истории);
 * периоды оплаты (генерация следующих периодов, список, отметка оплаты);
 * итоги по валютам, календарь, данные главной страницы;
 * пользователи, бюджеты, журнал действий, демо-дата.

Главные правила кейса, реализованные здесь:
 * срок следующего периода считается от «якорного дня» шаблона, а не прибавлением
   месяца к прошлой дате (31 янв -> 28 фев -> 31 мар, а не 28 мар);
 * нет такого числа в месяце -> последний день месяца;
 * просрочка не останавливает создание следующих периодов;
 * плановый срок (due_date) не меняется никогда;
 * удалённый регулярный платёж не создаёт новых периодов и напоминаний,
   но вся история оплат остаётся на месте.
"""
from __future__ import annotations

import calendar
import json
import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Optional

from app.config import settings
from app.database import execute, query_all, query_one, query_value, today as db_today, transaction
from app.schemas import (
    BudgetUpsert,
    PaymentOccurrenceUpdate,
    RecurringPaymentCreate,
    RecurringPaymentUpdate,
    UserCreate,
    UserUpdate,
)
from app.security import hash_password, revoke_sessions_of_user

log = logging.getLogger("invoice_keeper.services")

CURRENCIES = ("RUB", "USD", "EUR")
MAX_GENERATED_PERIODS = 120  # предохранитель от зацикливания (10 лет ежемесячных периодов)
ZERO = Decimal("0.00")


class ServiceError(Exception):
    """Ошибка бизнес-логики: API превращает её в HTTP-ответ."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class NotFoundError(ServiceError):
    def __init__(self, message: str = "Запись не найдена") -> None:
        super().__init__(message, status_code=404)


class ConflictError(ServiceError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=409)


# =============================================================================
# Даты периодов
# =============================================================================

def next_due_date(previous_due: date, anchor_day: int) -> date:
    """Следующий ежемесячный срок по якорному дню шаблона.

    День берётся из шаблона (anchor_day), а не из предыдущей даты — поэтому
    после 28 февраля снова будет 31 марта. Если в месяце нет такого числа,
    срок переносится на последний день месяца.
    """
    year, month = previous_due.year, previous_due.month
    month += 1
    if month > 12:
        month = 1
        year += 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(anchor_day, last_day))


def _insert_occurrence(conn: Any, payment: dict, due: date) -> int:
    """Создаёт период, если его ещё нет (защита от дублей — UNIQUE в базе)."""
    rowcount, _ = execute(
        "INSERT IGNORE INTO payment_occurrences (recurring_payment_id, amount, currency, due_date, is_paid) "
        "VALUES (%s, %s, %s, %s, FALSE)",
        (payment["id"], payment["amount"], payment["currency"], due),
        conn=conn,
    )
    return 1 if rowcount else 0


def ensure_periods_for_payment(conn: Any, payment: dict, today_: date) -> int:
    """Доводит периоды платежа до актуальных: все прошедшие плюс один будущий.

    Следующий период появляется независимо от того, оплачен ли предыдущий, —
    просрочка не останавливает регулярность (Слайд 5).
    """
    if payment["is_deleted"]:
        return 0

    created = 0
    last_due = query_value(
        "SELECT MAX(due_date) FROM payment_occurrences WHERE recurring_payment_id = %s",
        (payment["id"],),
        conn=conn,
    )
    if last_due is None:
        last_due = payment["start_date"]
        created += _insert_occurrence(conn, payment, last_due)

    guard = 0
    while last_due <= today_ and guard < MAX_GENERATED_PERIODS:
        guard += 1
        upcoming = next_due_date(last_due, payment["anchor_day"])
        created += _insert_occurrence(conn, payment, upcoming)
        last_due = upcoming
    return created


def ensure_all_periods() -> int:
    """Проверяет периоды всех активных платежей. Вызывается планировщиком и при старте."""
    created = 0
    with transaction() as conn:
        today_ = db_today(conn)
        payments = query_all(
            "SELECT id, amount, currency, start_date, anchor_day, is_deleted "
            "FROM recurring_payments WHERE is_deleted = FALSE",
            conn=conn,
        )
        for payment in payments:
            created += ensure_periods_for_payment(conn, payment, today_)
    if created:
        log.info("Создано новых периодов оплаты: %s", created)
    return created


# =============================================================================
# Регулярные платежи
# =============================================================================

_RECURRING_SELECT = """
SELECT rp.id, rp.name, rp.amount, rp.currency, rp.periodicity, rp.start_date, rp.anchor_day,
       rp.remind_days_before, rp.is_deleted, rp.deleted_at, rp.created_by, rp.created_at, rp.updated_at,
       (SELECT MIN(po.due_date) FROM payment_occurrences po
         WHERE po.recurring_payment_id = rp.id AND po.is_paid = FALSE) AS next_due_date,
       (SELECT COUNT(*) FROM payment_occurrences po
         WHERE po.recurring_payment_id = rp.id) AS periods_total,
       (SELECT COUNT(*) FROM payment_occurrences po
         WHERE po.recurring_payment_id = rp.id AND po.is_paid = FALSE) AS periods_unpaid
FROM recurring_payments rp
"""


def list_recurring_payments(
    *,
    include_deleted: bool = False,
    currency: Optional[str] = None,
    search: Optional[str] = None,
) -> list[dict]:
    where: list[str] = []
    params: list[Any] = []
    if not include_deleted:
        where.append("rp.is_deleted = FALSE")
    if currency:
        where.append("rp.currency = %s")
        params.append(currency)
    if search:
        where.append("rp.name LIKE %s")
        params.append(f"%{search}%")
    sql = _RECURRING_SELECT + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY rp.is_deleted ASC, next_due_date IS NULL, next_due_date ASC, rp.id ASC"
    return query_all(sql, params)


def get_recurring_payment(payment_id: int, *, conn: Any = None) -> dict:
    row = query_one(_RECURRING_SELECT + " WHERE rp.id = %s", (payment_id,), conn=conn)
    if row is None:
        raise NotFoundError("Регулярный платёж не найден")
    return row


def create_recurring_payment(payload: RecurringPaymentCreate, actor_username: str) -> dict:
    """Создаёт шаблон платежа и сразу его первый период.

    Если срок ближе, чем окно напоминания, период попадёт в ближайшую проверку
    планировщика и напоминание уйдёт сразу (Слайд 7).
    """
    anchor_day = payload.anchor_day or payload.start_date.day
    with transaction() as conn:
        _, payment_id = execute(
            "INSERT INTO recurring_payments "
            "(name, amount, currency, periodicity, start_date, anchor_day, remind_days_before, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                payload.name,
                payload.amount,
                payload.currency,
                payload.periodicity,
                payload.start_date,
                anchor_day,
                payload.remind_days_before,
                actor_username,
            ),
            conn=conn,
        )
        payment = query_one(
            "SELECT id, amount, currency, start_date, anchor_day, is_deleted FROM recurring_payments WHERE id = %s",
            (payment_id,),
            conn=conn,
        )
        ensure_periods_for_payment(conn, payment, db_today(conn))
        return get_recurring_payment(int(payment_id), conn=conn)


def _as_date(value: Any) -> Optional[date]:
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _month_key(value: date) -> tuple[int, int]:
    return value.year, value.month


def _due_in_month(year: int, month: int, anchor_day: int) -> date:
    """Срок в указанном месяце по якорному дню (нет такого числа — последний день месяца)."""
    return date(year, month, min(anchor_day, calendar.monthrange(year, month)[1]))


_EDITABLE_FIELDS = ("name", "amount", "currency", "periodicity", "start_date", "anchor_day", "remind_days_before")


def update_recurring_payment(payment_id: int, payload: RecurringPaymentUpdate) -> tuple[dict, dict]:
    """Изменение регулярного платежа администратором.

    Правила (история не переписывается никогда):
     * «открытые» периоды — неоплаченные со сроком сегодня или позже; оплаченные и
       прошедшие периоды остаются как были, со своими суммами и датами;
     * name, remind_days_before — меняются в шаблоне и сразу видны везде;
     * amount, currency — меняются в шаблоне и в открытых периодах; у этих периодов
       сбрасывается отметка о напоминании, чтобы бот прислал напоминание с новой суммой;
     * start_date, anchor_day — это смена графика: открытые периоды удаляются
       (менять due_date запрещает триггер базы) и создаются заново по новому графику.

    Возвращает (платёж с итогами изменений, {поле: (было, стало)} для журнала).
    """
    fields = payload.model_dump(exclude_unset=True)
    with transaction() as conn:
        today_ = db_today(conn)
        current = query_one(
            "SELECT id, name, amount, currency, periodicity, start_date, anchor_day, remind_days_before, is_deleted "
            "FROM recurring_payments WHERE id = %s FOR UPDATE",
            (payment_id,),
            conn=conn,
        )
        if current is None:
            raise NotFoundError("Регулярный платёж не найден")
        if current["is_deleted"]:
            raise ConflictError("Прекращённый платёж изменить нельзя: его история хранится в неизменном виде")
        current["start_date"] = _as_date(current["start_date"])
        current["amount"] = Decimal(str(current["amount"])).quantize(Decimal("0.01"))
        if "amount" in fields:
            fields["amount"] = fields["amount"].quantize(Decimal("0.01"))

        # Если меняется дата ближайшей оплаты, а день месяца не передан — он берётся из даты,
        # так же как при создании платежа. Та же дата, присланная повторно (форма целиком),
        # ничего не меняет.
        if "start_date" in fields and "anchor_day" not in fields and fields["start_date"] != current["start_date"]:
            fields["anchor_day"] = fields["start_date"].day

        changes: dict[str, tuple[Any, Any]] = {}
        for key in _EDITABLE_FIELDS:
            if key in fields and fields[key] != current[key]:
                changes[key] = (current[key], fields[key])

        result_counts = {"updated_periods": 0, "removed_periods": 0, "created_periods": 0}
        if not changes:
            payment = get_recurring_payment(payment_id, conn=conn)
            payment.update(result_counts, changed_fields=[])
            return payment, {}

        new = dict(current)
        new.update({key: after for key, (_, after) in changes.items()})
        schedule_changed = "start_date" in changes or "anchor_day" in changes
        money_changed = "amount" in changes or "currency" in changes

        # ---- Новый график проверяем ДО любых изменений в базе.
        open_rows: list[dict] = []
        new_first: Optional[date] = None
        if schedule_changed:
            open_rows = query_all(
                "SELECT id, due_date FROM payment_occurrences "
                "WHERE recurring_payment_id = %s AND is_paid = FALSE AND due_date >= %s ORDER BY due_date",
                (payment_id, today_),
                conn=conn,
            )
            kept_last = _as_date(query_value(
                "SELECT MAX(due_date) FROM payment_occurrences "
                "WHERE recurring_payment_id = %s AND NOT (is_paid = FALSE AND due_date >= %s)",
                (payment_id, today_),
                conn=conn,
            ))
            anchor = int(new["anchor_day"])
            if "start_date" in changes:
                new_first = new["start_date"]
                if kept_last is not None and _month_key(new_first) <= _month_key(kept_last):
                    raise ConflictError(
                        "Новая дата ближайшей оплаты должна быть в месяце после последнего сохранённого "
                        f"периода ({kept_last:%d.%m.%Y}): оплаченные и прошедшие периоды не переписываются"
                    )
            else:
                # Меняется только день месяца: ближайший срок — в том же месяце, где был
                # прежний ближайший неоплаченный период, но не в прошлом и не в месяце,
                # где уже есть сохранённый период.
                if open_rows:
                    base = _as_date(open_rows[0]["due_date"])
                elif kept_last is not None:
                    base = next_due_date(kept_last, anchor)
                else:
                    base = today_
                new_first = _due_in_month(base.year, base.month, anchor)
                guard = 0
                while (new_first < today_ or (kept_last is not None and _month_key(new_first) <= _month_key(kept_last))) \
                        and guard < MAX_GENERATED_PERIODS:
                    guard += 1
                    new_first = next_due_date(new_first, anchor)

        # ---- Шаблон.
        sets = [f"{key} = %s" for key in changes]
        params: list[Any] = [after for _, after in changes.values()]
        sets.append("updated_at = NOW()")
        params.append(payment_id)
        execute(f"UPDATE recurring_payments SET {', '.join(sets)} WHERE id = %s", params, conn=conn)

        # ---- Периоды.
        if schedule_changed:
            if open_rows:
                rowcount, _ = execute(
                    "DELETE FROM payment_occurrences "
                    "WHERE recurring_payment_id = %s AND is_paid = FALSE AND due_date >= %s",
                    (payment_id, today_),
                    conn=conn,
                )
                result_counts["removed_periods"] = int(rowcount or 0)
            payment_row = {
                "id": payment_id,
                "amount": new["amount"],
                "currency": new["currency"],
                "start_date": new["start_date"],
                "anchor_day": int(new["anchor_day"]),
                "is_deleted": False,
            }
            created = _insert_occurrence(conn, payment_row, new_first)
            created += ensure_periods_for_payment(conn, payment_row, today_)
            result_counts["created_periods"] = created
        elif money_changed:
            rowcount, _ = execute(
                "UPDATE payment_occurrences SET amount = %s, currency = %s, reminded_at = NULL, telegram_message_id = NULL "
                "WHERE recurring_payment_id = %s AND is_paid = FALSE AND due_date >= %s",
                (new["amount"], new["currency"], payment_id, today_),
                conn=conn,
            )
            result_counts["updated_periods"] = int(rowcount or 0)

        payment = get_recurring_payment(payment_id, conn=conn)
        payment.update(result_counts, changed_fields=list(changes))
        return payment, changes


def delete_recurring_payment_permanently(payment_id: int) -> tuple[dict, int]:
    """Полное удаление платежа вместе со всеми периодами (по явному запросу администратора).

    Нужно, чтобы убрать платёж, созданный по ошибке. Обычное удаление по ТЗ — мягкое
    (см. delete_recurring_payment). Возвращает (снимок платежа до удаления, сколько периодов удалено).
    """
    with transaction() as conn:
        exists = query_one("SELECT id FROM recurring_payments WHERE id = %s FOR UPDATE", (payment_id,), conn=conn)
        if exists is None:
            raise NotFoundError("Регулярный платёж не найден")
        snapshot = get_recurring_payment(payment_id, conn=conn)
        # Сначала периоды: внешний ключ ON DELETE RESTRICT не даёт удалить шаблон с историей.
        removed, _ = execute(
            "DELETE FROM payment_occurrences WHERE recurring_payment_id = %s", (payment_id,), conn=conn
        )
        execute("DELETE FROM recurring_payments WHERE id = %s", (payment_id,), conn=conn)
        return snapshot, int(removed or 0)


def delete_recurring_payment(payment_id: int) -> dict:
    """Прекращение платежа (мягкое удаление): напоминания и новые периоды прекращаются,
    история прошлых оплат остаётся (Слайд 13, критерий приёмки)."""
    with transaction() as conn:
        payment = query_one(
            "SELECT id, name, is_deleted FROM recurring_payments WHERE id = %s", (payment_id,), conn=conn
        )
        if payment is None:
            raise NotFoundError("Регулярный платёж не найден")
        if payment["is_deleted"]:
            raise ConflictError("Этот регулярный платёж уже прекращён")
        execute(
            "UPDATE recurring_payments SET is_deleted = TRUE, deleted_at = NOW() WHERE id = %s",
            (payment_id,),
            conn=conn,
        )
        return get_recurring_payment(payment_id, conn=conn)


# =============================================================================
# Периоды оплаты
# =============================================================================

_OCCURRENCE_SELECT = "SELECT * FROM v_payment_occurrences_detailed"


def _occurrence_filters(
    *,
    status: Optional[str],
    overdue: Optional[bool],
    currency: Optional[str],
    recurring_payment_id: Optional[int],
    date_from: Optional[date],
    date_to: Optional[date],
    include_deleted_recurring: bool,
    search: Optional[str],
    needs_reminder: Optional[bool] = None,
) -> tuple[str, list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    if status == "paid":
        where.append("is_paid = TRUE")
    elif status == "unpaid":
        where.append("is_paid = FALSE")
    if overdue is not None:
        where.append("is_overdue = TRUE" if overdue else "is_overdue = FALSE")
    if currency:
        where.append("currency = %s")
        params.append(currency)
    if recurring_payment_id is not None:
        where.append("recurring_payment_id = %s")
        params.append(recurring_payment_id)
    if date_from:
        where.append("due_date >= %s")
        params.append(date_from)
    if date_to:
        where.append("due_date <= %s")
        params.append(date_to)
    if not include_deleted_recurring:
        where.append("is_recurring_deleted = FALSE")
    if search:
        where.append("name LIKE %s")
        params.append(f"%{search}%")
    if needs_reminder is not None:
        where.append("needs_telegram_reminder = TRUE" if needs_reminder else "needs_telegram_reminder = FALSE")
    return (" WHERE " + " AND ".join(where) if where else ""), params


def list_occurrences(
    *,
    status: Optional[str] = None,
    overdue: Optional[bool] = None,
    currency: Optional[str] = None,
    recurring_payment_id: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    include_deleted_recurring: bool = True,
    search: Optional[str] = None,
    sort: str = "due_date",
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Список периодов с фильтрами. Возвращает (строки, общее количество)."""
    clause, params = _occurrence_filters(
        status=status,
        overdue=overdue,
        currency=currency,
        recurring_payment_id=recurring_payment_id,
        date_from=date_from,
        date_to=date_to,
        include_deleted_recurring=include_deleted_recurring,
        search=search,
    )
    order = {
        "due_date": "due_date ASC, id ASC",
        "-due_date": "due_date DESC, id DESC",
        "amount": "amount ASC, id ASC",
        "-amount": "amount DESC, id DESC",
        "name": "name ASC, due_date ASC",
    }.get(sort, "due_date ASC, id ASC")

    total = int(query_value(f"SELECT COUNT(*) FROM v_payment_occurrences_detailed{clause}", params) or 0)
    rows = query_all(
        f"{_OCCURRENCE_SELECT}{clause} ORDER BY {order} LIMIT %s OFFSET %s",
        [*params, limit, offset],
    )
    return rows, total


def get_occurrence(occurrence_id: int, *, conn: Any = None) -> dict:
    row = query_one(f"{_OCCURRENCE_SELECT} WHERE id = %s", (occurrence_id,), conn=conn)
    if row is None:
        raise NotFoundError("Период оплаты не найден")
    return row


def get_occurrence_safe(occurrence_id: int) -> Optional[dict]:
    """Период или None — без исключения. Нужен обработчику кнопки в Telegram."""
    return query_one(f"{_OCCURRENCE_SELECT} WHERE id = %s", (occurrence_id,))


def update_occurrence(
    occurrence_id: int,
    payload: PaymentOccurrenceUpdate,
    actor_username: str,
) -> tuple[dict, str]:
    """Отметка оплаты, её отмена и исправление фактической даты.

    Возвращает (обновлённый период, тип действия для журнала).
    Плановый срок не меняется — это гарантируют и этот код, и триггер в базе.
    """
    fields = payload.model_dump(exclude_unset=True)
    with transaction() as conn:
        today_ = db_today(conn)
        current = get_occurrence(occurrence_id, conn=conn)

        target_paid = bool(current["is_paid"]) if payload.is_paid is None else bool(payload.is_paid)
        new_date: Optional[date] = None

        if payload.is_paid is None and fields.get("actual_payment_date") is not None and not current["is_paid"]:
            raise ServiceError(
                "Сначала отметьте период оплаченным: фактическая дата указывается только для оплаченного периода"
            )

        if target_paid:
            if "actual_payment_date" in fields and fields["actual_payment_date"] is not None:
                new_date = payload.actual_payment_date
            elif current["actual_payment_date"]:
                new_date = current["actual_payment_date"]
            else:
                new_date = today_
            if new_date > today_:
                raise ServiceError("Фактическая дата оплаты не может быть в будущем")
        else:
            # Снятие отметки очищает фактическую дату (Слайд 5).
            new_date = None

        sets: list[str] = []
        params: list[Any] = []
        # Поля оплаты трогаем только если их прислали: правка примечания не должна
        # переписывать, кто и когда отметил оплату.
        if "is_paid" in fields or "actual_payment_date" in fields:
            sets += ["is_paid = %s", "actual_payment_date = %s", "paid_by_username = %s"]
            params += [target_paid, new_date, actor_username if target_paid else None]
        if "receipt_url" in fields:
            sets.append("receipt_url = %s")
            params.append(payload.receipt_url)
        if "notes" in fields:
            sets.append("notes = %s")
            params.append(payload.notes)

        if sets:
            params.append(occurrence_id)
            execute(f"UPDATE payment_occurrences SET {', '.join(sets)} WHERE id = %s", params, conn=conn)
        updated = get_occurrence(occurrence_id, conn=conn)

    was_paid = bool(current["is_paid"])
    if target_paid and not was_paid:
        action = "MARK_PAID"
    elif not target_paid and was_paid:
        action = "UNMARK_PAID"
    elif target_paid and current["actual_payment_date"] != updated["actual_payment_date"]:
        action = "CHANGE_PAID_DATE"
    else:
        action = "UPDATE_OCCURRENCE"
    return updated, action


def mark_paid_from_telegram(occurrence_id: int, actor_username: str) -> Optional[dict]:
    """Отметка оплаты кнопкой в Telegram (бонус, Слайд 16). Возвращает период или None."""
    with transaction() as conn:
        today_ = db_today(conn)
        row = query_one(
            "SELECT id, is_paid FROM payment_occurrences WHERE id = %s FOR UPDATE", (occurrence_id,), conn=conn
        )
        if row is None:
            return None
        if row["is_paid"]:
            return get_occurrence(occurrence_id, conn=conn)
        execute(
            "UPDATE payment_occurrences SET is_paid = TRUE, actual_payment_date = %s, paid_by_username = %s "
            "WHERE id = %s",
            (today_, actor_username, occurrence_id),
            conn=conn,
        )
        return get_occurrence(occurrence_id, conn=conn)


# =============================================================================
# Итоги по валютам (суммы считаются отдельно, без конвертации — Слайд 3)
# =============================================================================

_SUMMARY_AGGREGATE = """
SELECT currency,
       COUNT(*) AS total_periods_count,
       COALESCE(SUM(amount), 0.00) AS total_amount,
       COALESCE(SUM(CASE WHEN is_paid = TRUE THEN amount ELSE 0.00 END), 0.00) AS paid_amount,
       COALESCE(SUM(CASE WHEN is_paid = FALSE THEN amount ELSE 0.00 END), 0.00) AS unpaid_amount,
       COALESCE(SUM(CASE WHEN is_overdue = TRUE AND is_paid = FALSE THEN amount ELSE 0.00 END), 0.00)
           AS active_overdue_amount,
       COALESCE(SUM(CASE WHEN is_overdue = TRUE AND is_paid = TRUE THEN amount ELSE 0.00 END), 0.00)
           AS paid_with_overdue_amount,
       COUNT(CASE WHEN is_overdue = TRUE AND is_paid = FALSE THEN 1 END) AS active_overdue_count,
       COUNT(CASE WHEN is_paid = FALSE THEN 1 END) AS unpaid_count,
       COUNT(CASE WHEN is_paid = TRUE THEN 1 END) AS paid_count
FROM v_payment_occurrences_detailed
"""


def _empty_currency_row(currency: str) -> dict:
    return {
        "currency": currency,
        "total_periods_count": 0,
        "total_amount": ZERO,
        "paid_amount": ZERO,
        "unpaid_amount": ZERO,
        "active_overdue_amount": ZERO,
        "paid_with_overdue_amount": ZERO,
        "active_overdue_count": 0,
        "unpaid_count": 0,
        "paid_count": 0,
    }


def summary_by_currency(
    *,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    currency: Optional[str] = None,
    include_deleted_recurring: bool = True,
) -> list[dict]:
    """Итоги по каждой валюте. Всегда возвращает три строки: RUB, USD, EUR."""
    if date_from is None and date_to is None and include_deleted_recurring:
        # Готовое представление базы — ровно под GET /api/v1/payments/summary.
        rows = query_all("SELECT * FROM v_payments_summary_by_currency")
    else:
        clause, params = _occurrence_filters(
            status=None,
            overdue=None,
            currency=None,
            recurring_payment_id=None,
            date_from=date_from,
            date_to=date_to,
            include_deleted_recurring=include_deleted_recurring,
            search=None,
        )
        rows = query_all(f"{_SUMMARY_AGGREGATE}{clause} GROUP BY currency", params)

    by_currency = {row["currency"]: row for row in rows}
    result = [by_currency.get(code) or _empty_currency_row(code) for code in CURRENCIES]
    if currency:
        result = [row for row in result if row["currency"] == currency]
    return result


# =============================================================================
# Календарь и данные главной страницы
# =============================================================================

def calendar_month(year: int, month: int) -> dict:
    """Платежи по дням месяца — для календаря оплат (бонус, Слайд 16)."""
    if not 1 <= month <= 12:
        raise ServiceError("Месяц должен быть числом от 1 до 12")
    if not 2000 <= year <= 2100:
        raise ServiceError("Год вне допустимого диапазона")

    first_day = date(year, month, 1)
    last_day = date(year, month, calendar.monthrange(year, month)[1])
    today_ = db_today()
    rows, _ = list_occurrences(date_from=first_day, date_to=last_day, limit=1000)

    grouped: dict[date, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["due_date"], []).append(row)

    days = []
    for day_number in range(1, last_day.day + 1):
        day = date(year, month, day_number)
        items = grouped.get(day, [])
        days.append({
            "day": day,
            "is_today": day == today_,
            "total_count": len(items),
            "paid_count": sum(1 for item in items if item["is_paid"]),
            "unpaid_count": sum(1 for item in items if not item["is_paid"]),
            "overdue_count": sum(1 for item in items if item["is_overdue"]),
            "items": items,
        })
    return {"year": year, "month": month, "today": today_, "days": days}


def dashboard(user: dict) -> dict:
    """Всё, что нужно главной странице, одним запросом."""
    today_ = db_today()
    month_start = today_.replace(day=1)
    month_end = date(today_.year, today_.month, calendar.monthrange(today_.year, today_.month)[1])
    soon_border = today_ + timedelta(days=settings.default_remind_days_before)

    overdue, overdue_total = list_occurrences(status="unpaid", overdue=True, sort="due_date", limit=20)
    upcoming, _ = list_occurrences(
        status="unpaid", overdue=False, date_from=today_, date_to=today_ + timedelta(days=45),
        include_deleted_recurring=False, sort="due_date", limit=20,
    )
    recently_paid, _ = list_occurrences(status="paid", sort="-due_date", limit=10)

    counts = {
        "overdue": overdue_total,
        "due_soon": int(query_value(
            "SELECT COUNT(*) FROM v_payment_occurrences_detailed "
            "WHERE is_paid = FALSE AND is_recurring_deleted = FALSE AND due_date BETWEEN %s AND %s",
            (today_, soon_border),
        ) or 0),
        "unpaid": int(query_value(
            "SELECT COUNT(*) FROM v_payment_occurrences_detailed WHERE is_paid = FALSE"
        ) or 0),
        "paid": int(query_value(
            "SELECT COUNT(*) FROM v_payment_occurrences_detailed WHERE is_paid = TRUE"
        ) or 0),
        "active_recurring": int(query_value(
            "SELECT COUNT(*) FROM recurring_payments WHERE is_deleted = FALSE"
        ) or 0),
    }
    override = query_value("SELECT override_today FROM system_settings WHERE id = 1")
    return {
        "today": today_,
        "timezone": get_timezone(),
        "is_demo_date": override is not None,
        "user": user,
        "counts": counts,
        "month_summary": summary_by_currency(date_from=month_start, date_to=month_end),
        "total_summary": summary_by_currency(),
        "overdue": overdue,
        "upcoming": upcoming,
        "recently_paid": recently_paid,
    }


# =============================================================================
# Пользователи (управляет только администратор — Слайд 9)
# =============================================================================

_USER_SELECT = (
    "SELECT id, username, full_name, role, is_env_admin, is_active, telegram_user_id, created_at, updated_at "
    "FROM users"
)


def list_users(*, include_inactive: bool = True, search: Optional[str] = None) -> list[dict]:
    where: list[str] = []
    params: list[Any] = []
    if not include_inactive:
        where.append("is_active = TRUE")
    if search:
        where.append("(username LIKE %s OR full_name LIKE %s)")
        params.extend([f"%{search}%", f"%{search}%"])
    sql = _USER_SELECT + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id ASC"
    return query_all(sql, params)


def get_user(user_id: int, *, conn: Any = None) -> dict:
    row = query_one(f"{_USER_SELECT} WHERE id = %s", (user_id,), conn=conn)
    if row is None:
        raise NotFoundError("Пользователь не найден")
    return row


def get_user_by_username(username: str) -> Optional[dict]:
    return query_one(f"{_USER_SELECT} WHERE username = %s", (username,))


def _ensure_username_free(conn: Any, username: str, exclude_id: Optional[int] = None) -> None:
    if username == settings.admin_username:
        raise ConflictError("Этот логин занят учётной записью администратора из .env")
    row = query_one("SELECT id FROM users WHERE username = %s", (username,), conn=conn)
    if row and row["id"] != exclude_id:
        raise ConflictError("Пользователь с таким логином уже существует")


def create_user(payload: UserCreate) -> dict:
    with transaction() as conn:
        _ensure_username_free(conn, payload.username)
        if payload.telegram_user_id is not None:
            taken = query_one(
                "SELECT id FROM users WHERE telegram_user_id = %s", (payload.telegram_user_id,), conn=conn
            )
            if taken:
                raise ConflictError("Этот Telegram ID уже привязан к другому пользователю")
        _, user_id = execute(
            "INSERT INTO users (username, password_hash, full_name, role, is_env_admin, telegram_user_id, is_active) "
            "VALUES (%s, %s, %s, 'user', FALSE, %s, %s)",
            (
                payload.username,
                hash_password(payload.password),
                payload.full_name,
                payload.telegram_user_id,
                payload.is_active,
            ),
            conn=conn,
        )
        return get_user(int(user_id), conn=conn)


def update_user(user_id: int, payload: UserUpdate) -> dict:
    fields = payload.model_dump(exclude_unset=True)
    with transaction() as conn:
        existing = get_user(user_id, conn=conn)
        if existing["is_env_admin"]:
            raise ServiceError(
                "Учётная запись администратора задаётся в .env и через API не изменяется",
                status_code=403,
            )
        sets: list[str] = []
        params: list[Any] = []
        if "username" in fields:
            _ensure_username_free(conn, payload.username, exclude_id=user_id)
            sets.append("username = %s")
            params.append(payload.username)
        if "full_name" in fields:
            sets.append("full_name = %s")
            params.append(payload.full_name)
        if "telegram_user_id" in fields:
            if payload.telegram_user_id is not None:
                taken = query_one(
                    "SELECT id FROM users WHERE telegram_user_id = %s AND id <> %s",
                    (payload.telegram_user_id, user_id),
                    conn=conn,
                )
                if taken:
                    raise ConflictError("Этот Telegram ID уже привязан к другому пользователю")
            sets.append("telegram_user_id = %s")
            params.append(payload.telegram_user_id)
        if "is_active" in fields:
            sets.append("is_active = %s")
            params.append(payload.is_active)
        if "password" in fields:
            sets.append("password_hash = %s")
            params.append(hash_password(payload.password))

        if sets:
            params.append(user_id)
            execute(f"UPDATE users SET {', '.join(sets)} WHERE id = %s", params, conn=conn)
        # Смена пароля или отключение учётной записи завершает её активные сессии.
        if "password" in fields or fields.get("is_active") is False:
            revoke_sessions_of_user(user_id, conn=conn)
        return get_user(user_id, conn=conn)


def delete_user(user_id: int) -> dict:
    with transaction() as conn:
        existing = get_user(user_id, conn=conn)
        if existing["is_env_admin"]:
            raise ServiceError(
                "Учётную запись администратора из .env удалить нельзя",
                status_code=403,
            )
        execute("DELETE FROM users WHERE id = %s", (user_id,), conn=conn)
        return existing


# =============================================================================
# Бюджеты (бонус, Слайд 16)
# =============================================================================

def list_budgets(
    *, period_type: Optional[str] = None, year: Optional[int] = None, month: Optional[int] = None
) -> list[dict]:
    where: list[str] = []
    params: list[Any] = []
    if period_type:
        where.append("period_type = %s")
        params.append(period_type)
    if year is not None:
        where.append("period_year = %s")
        params.append(year)
    if month is not None:
        where.append("period_month = %s")
        params.append(month)
    sql = "SELECT * FROM budgets" + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY period_year DESC, period_month DESC, currency ASC"
    budgets = query_all(sql, params)

    for budget in budgets:
        if budget["period_type"] == "monthly":
            start = date(budget["period_year"], budget["period_month"], 1)
            end = date(
                budget["period_year"],
                budget["period_month"],
                calendar.monthrange(budget["period_year"], budget["period_month"])[1],
            )
        else:
            start = date(budget["period_year"], 1, 1)
            end = date(budget["period_year"], 12, 31)
        totals = query_one(
            "SELECT COALESCE(SUM(amount), 0.00) AS planned, "
            "       COALESCE(SUM(CASE WHEN is_paid = TRUE THEN amount ELSE 0.00 END), 0.00) AS spent "
            "FROM v_payment_occurrences_detailed WHERE currency = %s AND due_date BETWEEN %s AND %s",
            (budget["currency"], start, end),
        ) or {"planned": ZERO, "spent": ZERO}
        budget["planned_payments_amount"] = totals["planned"]
        budget["spent_amount"] = totals["spent"]
    return budgets


def upsert_budget(payload: BudgetUpsert) -> dict:
    with transaction() as conn:
        execute(
            "INSERT INTO budgets (period_type, period_year, period_month, currency, planned_limit) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE planned_limit = VALUES(planned_limit)",
            (
                payload.period_type,
                payload.period_year,
                payload.period_month,
                payload.currency,
                payload.planned_limit,
            ),
            conn=conn,
        )
    budgets = list_budgets(
        period_type=payload.period_type, year=payload.period_year, month=payload.period_month
    )
    for budget in budgets:
        if budget["currency"] == payload.currency:
            return budget
    raise NotFoundError("Бюджет не найден после сохранения")


def delete_budget(budget_id: int) -> None:
    rowcount, _ = execute("DELETE FROM budgets WHERE id = %s", (budget_id,))
    if not rowcount:
        raise NotFoundError("Бюджет не найден")


# =============================================================================
# Журнал действий (бонус, Слайд 16)
# =============================================================================

def list_audit_logs(
    *,
    actor_username: Optional[str] = None,
    action_type: Optional[str] = None,
    entity_type: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict], int]:
    where: list[str] = []
    params: list[Any] = []
    if actor_username:
        where.append("actor_username = %s")
        params.append(actor_username)
    if action_type:
        where.append("action_type = %s")
        params.append(action_type)
    if entity_type:
        where.append("entity_type = %s")
        params.append(entity_type)
    clause = " WHERE " + " AND ".join(where) if where else ""
    total = int(query_value(f"SELECT COUNT(*) FROM audit_logs{clause}", params) or 0)
    rows = query_all(
        f"SELECT * FROM audit_logs{clause} ORDER BY id DESC LIMIT %s OFFSET %s",
        [*params, limit, offset],
    )
    for row in rows:
        value = row.get("details")
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8")
        if isinstance(value, str):
            try:
                row["details"] = json.loads(value)
            except json.JSONDecodeError:
                row["details"] = {"raw": value}
    return rows, total


# =============================================================================
# Настройки и демо-дата
# =============================================================================

def get_timezone() -> str:
    value = query_value("SELECT timezone FROM system_settings WHERE id = 1")
    return value or settings.app_timezone


def get_settings_state() -> dict:
    row = query_one("SELECT timezone, override_today FROM system_settings WHERE id = 1") or {}
    return {
        "timezone": row.get("timezone") or settings.app_timezone,
        "today": db_today(),
        "override_today": row.get("override_today"),
        "real_today": query_value("SELECT CURDATE()"),
        "telegram_enabled": settings.telegram_enabled,
        "scheduler_enabled": settings.scheduler_enabled,
        "default_remind_days_before": settings.default_remind_days_before,
    }


def set_override_today(value: Optional[date]) -> dict:
    """Сдвиг «сегодня» для живой демонстрации: просрочка и напоминания пересчитываются сразу."""
    if value is not None and not 2000 <= value.year <= 2100:
        raise ServiceError("Дата вне допустимого диапазона")
    execute("UPDATE system_settings SET override_today = %s WHERE id = 1", (value,))
    ensure_all_periods()
    return get_settings_state()
