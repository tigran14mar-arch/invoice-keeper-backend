"""
Автономные тесты бизнес-логики (без MySQL — на SQLite-двойнике схемы).

Проверяют правила кейса:
 * якорный день: 31 янв -> 28 фев -> 31 мар, без сползания;
 * просрочка со следующего дня после срока, оплата в день срока — вовремя;
 * исправление фактической даты пересчитывает просрочку, срок не меняется;
 * следующий период создаётся даже при просрочке предыдущего;
 * прекращённый платёж не создаёт периодов и напоминаний, история цела;
 * напоминания минимум за 3 дня, без дублей;
 * итоги считаются отдельно по RUB, USD, EUR;
 * пользователь не может то, что может только администратор.

Запуск:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import unittest
from datetime import date, timedelta
from decimal import Decimal

from tests import sqlite_double

sqlite_double.install()

from app import services  # noqa: E402
from app.schemas import (  # noqa: E402
    BudgetUpsert,
    PaymentOccurrenceUpdate,
    RecurringPaymentCreate,
    UserCreate,
    UserUpdate,
)
from app.security import (  # noqa: E402
    authenticate,
    create_session,
    revoke_session,
    sync_env_admin,
    _token_fingerprint,
)

TODAY = date(2026, 10, 24)


def make_payment(**kwargs) -> dict:
    data = {
        "name": "Аренда офиса",
        "amount": Decimal("90000.00"),
        "currency": "RUB",
        "start_date": TODAY + timedelta(days=2),
    }
    data.update(kwargs)
    return services.create_recurring_payment(RecurringPaymentCreate(**data), "admin")


def due_dates(payment_id: int) -> list[date]:
    rows, _ = services.list_occurrences(recurring_payment_id=payment_id, limit=100)
    return [row["due_date"] for row in rows]


class BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        sqlite_double.reset(TODAY)


# =============================================================================
# Периоды и якорный день
# =============================================================================

class Periods(BaseCase):
    def test_first_period_created_with_payment(self):
        payment = make_payment()
        self.assertEqual(due_dates(payment["id"]), [TODAY + timedelta(days=2)])
        self.assertEqual(payment["anchor_day"], (TODAY + timedelta(days=2)).day)
        self.assertEqual(payment["next_due_date"], TODAY + timedelta(days=2))

    def test_anchor_day_does_not_drift(self):
        """31 янв -> 28 фев -> 31 мар, а не 28 мар (Слайд 4, «Критичные места» п.2)."""
        sqlite_double.reset(date(2026, 3, 15))
        payment = make_payment(start_date=date(2026, 1, 31), anchor_day=31)
        self.assertEqual(
            due_dates(payment["id"]),
            [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)],
        )

    def test_next_period_exists_even_when_previous_is_overdue(self):
        payment = make_payment(start_date=date(2026, 9, 20))
        dates = due_dates(payment["id"])
        self.assertEqual(dates, [date(2026, 9, 20), date(2026, 10, 20), date(2026, 11, 20)])
        rows, _ = services.list_occurrences(recurring_payment_id=payment["id"])
        self.assertTrue(rows[0]["is_overdue"] and not rows[0]["is_paid"])
        self.assertFalse(rows[2]["is_overdue"])  # будущий период со своим статусом

    def test_ensure_all_periods_is_idempotent(self):
        payment = make_payment(start_date=date(2026, 9, 20))
        before = due_dates(payment["id"])
        self.assertEqual(services.ensure_all_periods(), 0)
        self.assertEqual(due_dates(payment["id"]), before)

    def test_february_29_in_leap_year(self):
        sqlite_double.reset(date(2028, 3, 1))
        payment = make_payment(start_date=date(2028, 1, 31), anchor_day=31)
        self.assertIn(date(2028, 2, 29), due_dates(payment["id"]))


# =============================================================================
# Просрочка
# =============================================================================

class Overdue(BaseCase):
    def test_unpaid_after_due_is_overdue(self):
        payment = make_payment(start_date=TODAY - timedelta(days=3))
        row = services.list_occurrences(recurring_payment_id=payment["id"])[0][0]
        self.assertTrue(row["is_overdue"])
        self.assertEqual(row["overdue_days"], 3)

    def test_paid_on_due_date_is_on_time(self):
        payment = make_payment(start_date=TODAY - timedelta(days=3))
        occurrence = services.list_occurrences(recurring_payment_id=payment["id"])[0][0]
        updated, action = services.update_occurrence(
            occurrence["id"],
            PaymentOccurrenceUpdate(is_paid=True, actual_payment_date=occurrence["due_date"]),
            "ivan",
        )
        self.assertEqual(action, "MARK_PAID")
        self.assertFalse(updated["is_overdue"])
        self.assertEqual(updated["overdue_days"], 0)

    def test_paid_later_than_due_is_overdue(self):
        payment = make_payment(start_date=TODAY - timedelta(days=5))
        occurrence = services.list_occurrences(recurring_payment_id=payment["id"])[0][0]
        updated, _ = services.update_occurrence(
            occurrence["id"],
            PaymentOccurrenceUpdate(is_paid=True, actual_payment_date=TODAY - timedelta(days=2)),
            "ivan",
        )
        self.assertTrue(updated["is_overdue"])
        self.assertEqual(updated["overdue_days"], 3)

    def test_fixing_the_date_removes_wrong_overdue_and_keeps_due_date(self):
        """Исправили фактическую дату — просрочка пересчиталась, срок не изменился (Слайд 5)."""
        payment = make_payment(start_date=TODAY - timedelta(days=5))
        occurrence = services.list_occurrences(recurring_payment_id=payment["id"])[0][0]
        due = occurrence["due_date"]
        services.update_occurrence(
            occurrence["id"],
            PaymentOccurrenceUpdate(is_paid=True, actual_payment_date=TODAY - timedelta(days=1)),
            "ivan",
        )
        fixed, action = services.update_occurrence(
            occurrence["id"], PaymentOccurrenceUpdate(actual_payment_date=due), "ivan"
        )
        self.assertEqual(action, "CHANGE_PAID_DATE")
        self.assertFalse(fixed["is_overdue"])
        self.assertEqual(fixed["overdue_days"], 0)
        self.assertEqual(fixed["due_date"], due)

    def test_demo_date_shift_creates_overdue(self):
        """Сдвиг «сегодня» показывает просрочку без ожидания (демо для жюри)."""
        payment = make_payment(start_date=TODAY + timedelta(days=2))
        row = services.list_occurrences(recurring_payment_id=payment["id"])[0][0]
        self.assertFalse(row["is_overdue"])
        services.set_override_today(TODAY + timedelta(days=10))
        row = services.list_occurrences(recurring_payment_id=payment["id"])[0][0]
        self.assertTrue(row["is_overdue"])
        self.assertEqual(row["overdue_days"], 8)
        services.set_override_today(None)
        self.assertFalse(services.list_occurrences(recurring_payment_id=payment["id"])[0][0]["is_overdue"])


# =============================================================================
# Отметка оплаты (PATCH)
# =============================================================================

class MarkPayment(BaseCase):
    def setUp(self) -> None:
        super().setUp()
        self.payment = make_payment(start_date=TODAY)
        self.occurrence = services.list_occurrences(recurring_payment_id=self.payment["id"])[0][0]

    def test_mark_paid_without_date_uses_today(self):
        updated, _ = services.update_occurrence(
            self.occurrence["id"], PaymentOccurrenceUpdate(is_paid=True), "ivan"
        )
        self.assertTrue(updated["is_paid"])
        self.assertEqual(updated["actual_payment_date"], TODAY)
        self.assertEqual(updated["paid_by_username"], "ivan")
        self.assertEqual(updated["status"], "paid")

    def test_unmark_clears_actual_date(self):
        services.update_occurrence(self.occurrence["id"], PaymentOccurrenceUpdate(is_paid=True), "ivan")
        updated, action = services.update_occurrence(
            self.occurrence["id"], PaymentOccurrenceUpdate(is_paid=False), "ivan"
        )
        self.assertEqual(action, "UNMARK_PAID")
        self.assertFalse(updated["is_paid"])
        self.assertIsNone(updated["actual_payment_date"])

    def test_date_without_paid_status_is_rejected(self):
        with self.assertRaises(services.ServiceError) as ctx:
            services.update_occurrence(
                self.occurrence["id"], PaymentOccurrenceUpdate(actual_payment_date=TODAY), "ivan"
            )
        self.assertEqual(ctx.exception.status_code, 400)

    def test_future_payment_date_is_rejected(self):
        with self.assertRaises(services.ServiceError):
            services.update_occurrence(
                self.occurrence["id"],
                PaymentOccurrenceUpdate(is_paid=True, actual_payment_date=TODAY + timedelta(days=1)),
                "ivan",
            )

    def test_unpaid_with_date_is_rejected_by_schema(self):
        with self.assertRaises(ValueError):
            PaymentOccurrenceUpdate(is_paid=False, actual_payment_date=TODAY)

    def test_empty_patch_is_rejected_by_schema(self):
        with self.assertRaises(ValueError):
            PaymentOccurrenceUpdate()

    def test_unknown_field_is_rejected_by_schema(self):
        with self.assertRaises(ValueError):
            PaymentOccurrenceUpdate(is_paid=True, due_date=TODAY)

    def test_marking_one_period_does_not_touch_others(self):
        """Отметка — только для конкретного периода (Слайд 7)."""
        payment = make_payment(name="Серверы", currency="USD", amount=Decimal("165.00"),
                               start_date=date(2026, 9, 21))
        rows, _ = services.list_occurrences(recurring_payment_id=payment["id"])
        services.update_occurrence(rows[0]["id"], PaymentOccurrenceUpdate(is_paid=True), "ivan")
        rows_after, _ = services.list_occurrences(recurring_payment_id=payment["id"])
        self.assertTrue(rows_after[0]["is_paid"])
        self.assertFalse(rows_after[1]["is_paid"])
        self.assertFalse(rows_after[2]["is_paid"])

    def test_editing_note_keeps_who_paid(self):
        services.update_occurrence(self.occurrence["id"], PaymentOccurrenceUpdate(is_paid=True), "ivan")
        updated, action = services.update_occurrence(
            self.occurrence["id"], PaymentOccurrenceUpdate(notes="чек в бухгалтерии"), "maria"
        )
        self.assertEqual(action, "UPDATE_OCCURRENCE")
        self.assertEqual(updated["paid_by_username"], "ivan")  # не переписали, кто отметил оплату
        self.assertEqual(updated["notes"], "чек в бухгалтерии")
        self.assertTrue(updated["is_paid"])
        self.assertEqual(updated["actual_payment_date"], TODAY)

    def test_receipt_url_is_saved(self):
        updated, _ = services.update_occurrence(
            self.occurrence["id"],
            PaymentOccurrenceUpdate(is_paid=True, receipt_url="https://example.com/receipt.pdf"),
            "ivan",
        )
        self.assertEqual(updated["receipt_url"], "https://example.com/receipt.pdf")

    def test_missing_occurrence_is_404(self):
        with self.assertRaises(services.NotFoundError):
            services.update_occurrence(999, PaymentOccurrenceUpdate(is_paid=True), "ivan")


# =============================================================================
# Прекращение регулярного платежа
# =============================================================================

class SoftDelete(BaseCase):
    def test_history_survives_and_no_new_periods(self):
        payment = make_payment(start_date=date(2026, 9, 20))
        occurrences_before = due_dates(payment["id"])
        services.update_occurrence(
            services.list_occurrences(recurring_payment_id=payment["id"])[0][0]["id"],
            PaymentOccurrenceUpdate(is_paid=True),
            "ivan",
        )
        deleted = services.delete_recurring_payment(payment["id"])
        self.assertTrue(deleted["is_deleted"])
        self.assertIsNotNone(deleted["deleted_at"])

        self.assertEqual(services.ensure_all_periods(), 0)
        self.assertEqual(due_dates(payment["id"]), occurrences_before)
        rows, _ = services.list_occurrences(recurring_payment_id=payment["id"])
        self.assertTrue(rows[0]["is_paid"])  # история оплат сохранена
        self.assertTrue(all(row["is_recurring_deleted"] for row in rows))

    def test_deleted_payment_hidden_from_default_list(self):
        payment = make_payment()
        services.delete_recurring_payment(payment["id"])
        self.assertEqual(services.list_recurring_payments(), [])
        self.assertEqual(len(services.list_recurring_payments(include_deleted=True)), 1)

    def test_second_delete_is_conflict(self):
        payment = make_payment()
        services.delete_recurring_payment(payment["id"])
        with self.assertRaises(services.ConflictError):
            services.delete_recurring_payment(payment["id"])

    def test_delete_missing_payment_is_404(self):
        with self.assertRaises(services.NotFoundError):
            services.delete_recurring_payment(999)


# =============================================================================
# Напоминания
# =============================================================================

def reminder_candidates() -> list[dict]:
    return sqlite_double.query_all(
        "SELECT id, name FROM v_payment_occurrences_detailed WHERE needs_telegram_reminder = TRUE"
    )


class Reminders(BaseCase):
    def test_reminder_three_days_before_due(self):
        make_payment(start_date=TODAY + timedelta(days=3))
        self.assertEqual(len(reminder_candidates()), 1)

    def test_no_reminder_earlier_than_window(self):
        make_payment(start_date=TODAY + timedelta(days=4))
        self.assertEqual(reminder_candidates(), [])

    def test_payment_added_late_is_reminded_immediately(self):
        """Добавили платёж позже, чем за 3 дня до срока -> напоминание сразу (Слайд 7)."""
        make_payment(start_date=TODAY + timedelta(days=1))
        self.assertEqual(len(reminder_candidates()), 1)

    def test_paid_period_is_not_reminded(self):
        payment = make_payment(start_date=TODAY + timedelta(days=2))
        occurrence = services.list_occurrences(recurring_payment_id=payment["id"])[0][0]
        services.update_occurrence(occurrence["id"], PaymentOccurrenceUpdate(is_paid=True), "ivan")
        self.assertEqual(reminder_candidates(), [])

    def test_no_duplicate_after_reminder_sent(self):
        make_payment(start_date=TODAY + timedelta(days=2))
        occurrence_id = reminder_candidates()[0]["id"]
        rowcount, _ = sqlite_double.execute(
            "UPDATE payment_occurrences SET reminded_at = NOW() WHERE id = %s AND reminded_at IS NULL",
            (occurrence_id,),
        )
        self.assertEqual(rowcount, 1)
        self.assertEqual(reminder_candidates(), [])
        # повторная «бронь» не проходит — второго сообщения не будет
        rowcount, _ = sqlite_double.execute(
            "UPDATE payment_occurrences SET reminded_at = NOW() WHERE id = %s AND reminded_at IS NULL",
            (occurrence_id,),
        )
        self.assertEqual(rowcount, 0)

    def test_deleted_payment_is_not_reminded(self):
        payment = make_payment(start_date=TODAY + timedelta(days=2))
        self.assertEqual(len(reminder_candidates()), 1)
        services.delete_recurring_payment(payment["id"])
        self.assertEqual(reminder_candidates(), [])

    def test_overdue_unpaid_period_still_needs_reminder(self):
        make_payment(start_date=TODAY - timedelta(days=2))
        self.assertTrue(reminder_candidates())


# =============================================================================
# Итоги по валютам
# =============================================================================

class Summary(BaseCase):
    def test_always_three_currencies_even_when_empty(self):
        rows = services.summary_by_currency()
        self.assertEqual([row["currency"] for row in rows], ["RUB", "USD", "EUR"])
        self.assertTrue(all(Decimal(row["total_amount"]) == 0 for row in rows))

    def test_amounts_are_counted_separately_per_currency(self):
        later = TODAY + timedelta(days=2)
        make_payment(name="Аренда", currency="RUB", amount=Decimal("90000.00"), start_date=later)
        make_payment(name="Серверы", currency="USD", amount=Decimal("165.00"), start_date=later)
        make_payment(name="Подписка", currency="EUR", amount=Decimal("110.00"), start_date=later)
        rows = {row["currency"]: row for row in services.summary_by_currency()}
        self.assertEqual(Decimal(rows["RUB"]["total_amount"]), Decimal("90000.00"))
        self.assertEqual(Decimal(rows["USD"]["total_amount"]), Decimal("165.00"))
        self.assertEqual(Decimal(rows["EUR"]["total_amount"]), Decimal("110.00"))

    def test_paid_unpaid_and_overdue_totals(self):
        payment = make_payment(currency="RUB", amount=Decimal("1000.00"), start_date=date(2026, 9, 20))
        rows, _ = services.list_occurrences(recurring_payment_id=payment["id"])
        services.update_occurrence(rows[0]["id"], PaymentOccurrenceUpdate(is_paid=True), "ivan")
        summary = {row["currency"]: row for row in services.summary_by_currency()}["RUB"]
        self.assertEqual(Decimal(summary["total_amount"]), Decimal("3000.00"))
        self.assertEqual(Decimal(summary["paid_amount"]), Decimal("1000.00"))
        self.assertEqual(Decimal(summary["unpaid_amount"]), Decimal("2000.00"))
        self.assertEqual(Decimal(summary["active_overdue_amount"]), Decimal("1000.00"))
        self.assertEqual(summary["active_overdue_count"], 1)
        self.assertEqual(Decimal(summary["paid_with_overdue_amount"]), Decimal("1000.00"))

    def test_date_range_filter(self):
        make_payment(currency="RUB", amount=Decimal("500.00"), start_date=date(2026, 9, 20))
        rows = {row["currency"]: row for row in services.summary_by_currency(
            date_from=date(2026, 10, 1), date_to=date(2026, 10, 31))}
        self.assertEqual(Decimal(rows["RUB"]["total_amount"]), Decimal("500.00"))
        self.assertEqual(rows["RUB"]["total_periods_count"], 1)


# =============================================================================
# Списки, фильтры, календарь, дашборд
# =============================================================================

class Lists(BaseCase):
    def setUp(self) -> None:
        super().setUp()
        self.rub = make_payment(name="Аренда", currency="RUB", start_date=date(2026, 9, 20))
        self.usd = make_payment(name="Серверы", currency="USD", amount=Decimal("165.00"), start_date=TODAY)

    def test_filter_by_status_currency_and_overdue(self):
        rows, _ = services.list_occurrences(status="unpaid")
        self.assertTrue(all(not row["is_paid"] for row in rows))
        rows, _ = services.list_occurrences(currency="USD")
        self.assertTrue(all(row["currency"] == "USD" for row in rows))
        rows, total = services.list_occurrences(overdue=True)
        self.assertEqual(total, 2)  # два прошедших периода аренды

    def test_pagination_and_total(self):
        rows, total = services.list_occurrences(limit=2, offset=0)
        self.assertEqual(len(rows), 2)
        # аренда: 20.09, 20.10, 20.11; серверы: 24.10 и следующий 24.11
        self.assertEqual(total, 5)
        rows2, _ = services.list_occurrences(limit=2, offset=2)
        self.assertEqual(len(rows2), 2)
        self.assertNotEqual(rows[0]["id"], rows2[0]["id"])

    def test_sorted_by_due_date(self):
        rows, _ = services.list_occurrences()
        self.assertEqual([row["due_date"] for row in rows], sorted(row["due_date"] for row in rows))

    def test_calendar_month(self):
        result = services.calendar_month(2026, 10)
        self.assertEqual(len(result["days"]), 31)
        day = next(item for item in result["days"] if item["day"] == date(2026, 10, 20))
        self.assertEqual(day["total_count"], 1)
        self.assertEqual(day["overdue_count"], 1)
        self.assertTrue(next(item for item in result["days"] if item["day"] == TODAY)["is_today"])

    def test_calendar_rejects_bad_month(self):
        with self.assertRaises(services.ServiceError):
            services.calendar_month(2026, 13)

    def test_dashboard(self):
        data = services.dashboard({"id": 1, "username": "admin", "full_name": None, "role": "admin",
                                   "is_env_admin": True, "is_active": True, "telegram_user_id": None,
                                   "created_at": None, "updated_at": None})
        self.assertEqual(data["today"], TODAY)
        self.assertEqual(data["counts"]["overdue"], 2)
        self.assertEqual(data["counts"]["active_recurring"], 2)
        self.assertEqual(len(data["month_summary"]), 3)
        self.assertFalse(data["is_demo_date"])


# =============================================================================
# Пользователи, администратор из .env, сессии
# =============================================================================

class Users(BaseCase):
    def setUp(self) -> None:
        super().setUp()
        sync_env_admin()

    def test_env_admin_is_created_and_protected(self):
        admin = services.get_user_by_username("admin")
        self.assertIsNotNone(admin)
        self.assertEqual(admin["role"], "admin")
        self.assertTrue(admin["is_env_admin"])
        with self.assertRaises(services.ServiceError) as ctx:
            services.update_user(admin["id"], UserUpdate(full_name="Взлом"))
        self.assertEqual(ctx.exception.status_code, 403)
        with self.assertRaises(services.ServiceError) as ctx:
            services.delete_user(admin["id"])
        self.assertEqual(ctx.exception.status_code, 403)

    def test_create_user_has_role_user(self):
        user = services.create_user(UserCreate(username="ivan", password="ivanpass123", full_name="Иван"))
        self.assertEqual(user["role"], "user")
        self.assertFalse(user["is_env_admin"])
        self.assertNotIn("password_hash", user)

    def test_duplicate_login_is_conflict(self):
        services.create_user(UserCreate(username="ivan", password="ivanpass123"))
        with self.assertRaises(services.ConflictError):
            services.create_user(UserCreate(username="ivan", password="otherpass1"))
        with self.assertRaises(services.ConflictError):
            services.create_user(UserCreate(username="admin", password="otherpass1"))

    def test_update_and_delete_user(self):
        user = services.create_user(UserCreate(username="ivan", password="ivanpass123"))
        updated = services.update_user(user["id"], UserUpdate(full_name="Иван Петров", is_active=False))
        self.assertEqual(updated["full_name"], "Иван Петров")
        self.assertFalse(updated["is_active"])
        services.delete_user(user["id"])
        with self.assertRaises(services.NotFoundError):
            services.get_user(user["id"])

    def test_password_rules(self):
        with self.assertRaises(ValueError):
            UserCreate(username="ivan", password="short1")
        with self.assertRaises(ValueError):
            UserCreate(username="ivan", password="12345678")
        with self.assertRaises(ValueError):
            UserCreate(username="и", password="ivanpass123")

    def test_authenticate_admin_from_env_and_regular_user(self):
        self.assertIsNotNone(authenticate("admin", "admin12345"))
        self.assertIsNone(authenticate("admin", "wrong"))
        services.create_user(UserCreate(username="ivan", password="ivanpass123"))
        self.assertIsNotNone(authenticate("ivan", "ivanpass123"))
        self.assertIsNone(authenticate("ivan", "wrong"))
        self.assertIsNone(authenticate("nobody", "ivanpass123"))

    def test_inactive_user_cannot_login(self):
        user = services.create_user(UserCreate(username="ivan", password="ivanpass123"))
        services.update_user(user["id"], UserUpdate(is_active=False))
        self.assertIsNone(authenticate("ivan", "ivanpass123"))

    def test_session_lifecycle(self):
        user = authenticate("admin", "admin12345")
        token, expires_at = create_session(user)
        row = sqlite_double.query_one(
            "SELECT username, role FROM user_sessions WHERE session_token = %s AND is_revoked = FALSE "
            "AND expires_at > NOW()",
            (_token_fingerprint(token),),
        )
        self.assertEqual(row["username"], "admin")
        revoke_session(_token_fingerprint(token))
        row = sqlite_double.query_one(
            "SELECT username FROM user_sessions WHERE session_token = %s AND is_revoked = FALSE",
            (_token_fingerprint(token),),
        )
        self.assertIsNone(row)

    def test_password_change_revokes_sessions(self):
        user = services.create_user(UserCreate(username="ivan", password="ivanpass123"))
        token, _ = create_session(services.get_user(user["id"]) | {"username": "ivan", "role": "user"})
        services.update_user(user["id"], UserUpdate(password="newpass1234"))
        row = sqlite_double.query_one(
            "SELECT id FROM user_sessions WHERE session_token = %s AND is_revoked = FALSE",
            (_token_fingerprint(token),),
        )
        self.assertIsNone(row)


# =============================================================================
# Бюджеты и журнал (бонусы)
# =============================================================================

class Extras(BaseCase):
    def test_budget_upsert_and_facts(self):
        make_payment(currency="RUB", amount=Decimal("1000.00"), start_date=TODAY)
        budget = services.upsert_budget(BudgetUpsert(
            period_type="monthly", period_year=2026, period_month=10,
            currency="RUB", planned_limit=Decimal("5000.00"),
        ))
        self.assertEqual(Decimal(budget["planned_limit"]), Decimal("5000.00"))
        self.assertEqual(Decimal(budget["planned_payments_amount"]), Decimal("1000.00"))
        self.assertEqual(Decimal(budget["spent_amount"]), Decimal("0.00"))
        again = services.upsert_budget(BudgetUpsert(
            period_type="monthly", period_year=2026, period_month=10,
            currency="RUB", planned_limit=Decimal("7000.00"),
        ))
        self.assertEqual(Decimal(again["planned_limit"]), Decimal("7000.00"))
        self.assertEqual(len(services.list_budgets(period_type="monthly", year=2026, month=10)), 1)

    def test_budget_month_rules(self):
        with self.assertRaises(ValueError):
            BudgetUpsert(period_type="monthly", period_year=2026, period_month=0,
                         currency="RUB", planned_limit=Decimal("1"))
        with self.assertRaises(ValueError):
            BudgetUpsert(period_type="yearly", period_year=2026, period_month=5,
                         currency="RUB", planned_limit=Decimal("1"))

    def test_audit_log_is_written_and_read(self):
        from app.security import log_action

        log_action("admin", "admin", "LOGIN", details={"ip": "127.0.0.1"}, ip_address="127.0.0.1")
        rows, total = services.list_audit_logs()
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["action_type"], "LOGIN")
        self.assertEqual(rows[0]["details"], {"ip": "127.0.0.1"})

    def test_amount_validation(self):
        with self.assertRaises(ValueError):
            RecurringPaymentCreate(name="X", amount=Decimal("0"), currency="RUB", start_date=TODAY)
        with self.assertRaises(ValueError):
            RecurringPaymentCreate(name="X", amount=Decimal("1.005"), currency="RUB", start_date=TODAY)
        with self.assertRaises(ValueError):
            RecurringPaymentCreate(name="X", amount=Decimal("10"), currency="GBP", start_date=TODAY)
        with self.assertRaises(ValueError):
            RecurringPaymentCreate(name="X", amount=Decimal("10"), currency="RUB",
                                   start_date=TODAY, remind_days_before=1)
        with self.assertRaises(ValueError):
            RecurringPaymentCreate(name="   ", amount=Decimal("10"), currency="RUB", start_date=TODAY)


if __name__ == "__main__":
    unittest.main(verbosity=2)
