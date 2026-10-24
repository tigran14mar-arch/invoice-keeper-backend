"""
Тесты изменения и удаления регулярных платежей администратором.

PATCH  /api/v1/recurring-payments/{id}
DELETE /api/v1/recurring-payments/{id}?permanent=true

Сегодня в тестах — 24.10.2026 (как в остальных тестах).
Запуск:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from pydantic import ValidationError

from tests import sqlite_double

sqlite_double.install()

from app import services  # noqa: E402
from app.schemas import PaymentOccurrenceUpdate, RecurringPaymentCreate, RecurringPaymentUpdate  # noqa: E402

TODAY = date(2026, 10, 24)


def make_payment(**kwargs) -> dict:
    data = {
        "name": "Аренда офиса",
        "amount": Decimal("90000.00"),
        "currency": "RUB",
        "start_date": date(2026, 9, 20),  # периоды: 20.09 и 20.10 (прошли), 20.11 (будущий)
    }
    data.update(kwargs)
    return services.create_recurring_payment(RecurringPaymentCreate(**data), "admin")


def edit(payment_id: int, **fields) -> dict:
    payment, _ = services.update_recurring_payment(payment_id, RecurringPaymentUpdate(**fields))
    return payment


def periods(payment_id: int) -> list[dict]:
    rows, _ = services.list_occurrences(recurring_payment_id=payment_id, limit=500)
    return rows


def due_dates(payment_id: int) -> list[date]:
    return [row["due_date"] for row in periods(payment_id)]


def mark_paid(occurrence_id: int) -> None:
    services.update_occurrence(occurrence_id, PaymentOccurrenceUpdate(is_paid=True), "ivan")


class BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        sqlite_double.reset(TODAY)


class EditSimpleFields(BaseCase):
    def test_rename_touches_only_name(self):
        payment = make_payment()
        before = periods(payment["id"])
        updated = edit(payment["id"], name="  Аренда (новый БЦ)  ")
        self.assertEqual(updated["name"], "Аренда (новый БЦ)")
        self.assertEqual(updated["changed_fields"], ["name"])
        self.assertEqual(updated["removed_periods"] + updated["created_periods"] + updated["updated_periods"], 0)
        after = periods(payment["id"])
        self.assertEqual([r["id"] for r in after], [r["id"] for r in before])
        self.assertTrue(all(r["name"] == "Аренда (новый БЦ)" for r in after))

    def test_remind_days_before(self):
        payment = make_payment(start_date=date(2026, 10, 29))  # через 5 дней
        self.assertFalse(periods(payment["id"])[0]["needs_telegram_reminder"])
        updated = edit(payment["id"], remind_days_before=7)
        self.assertEqual(updated["remind_days_before"], 7)
        self.assertTrue(periods(payment["id"])[0]["needs_telegram_reminder"])

    def test_same_values_is_noop(self):
        payment = make_payment()
        updated = edit(payment["id"], name="Аренда офиса", amount=Decimal("90000"), anchor_day=20)
        self.assertEqual(updated["changed_fields"], [])
        self.assertEqual(due_dates(payment["id"]), [date(2026, 9, 20), date(2026, 10, 20), date(2026, 11, 20)])


class EditMoney(BaseCase):
    def test_amount_changes_only_future_unpaid_periods(self):
        payment = make_payment()
        rows = periods(payment["id"])
        mark_paid(rows[0]["id"])  # 20.09 — оплачен
        updated = edit(payment["id"], amount=Decimal("100000.50"))
        self.assertEqual(updated["amount"], Decimal("100000.50"))
        self.assertEqual(updated["updated_periods"], 1)
        amounts = {r["due_date"]: r["amount"] for r in periods(payment["id"])}
        self.assertEqual(amounts[date(2026, 9, 20)], Decimal("90000.00"))   # оплаченный — история
        self.assertEqual(amounts[date(2026, 10, 20)], Decimal("90000.00"))  # прошедший неоплаченный — история
        self.assertEqual(amounts[date(2026, 11, 20)], Decimal("100000.50"))  # будущий — новая сумма

    def test_period_due_today_is_updated(self):
        payment = make_payment(start_date=TODAY)
        edit(payment["id"], amount=Decimal("1.00"))
        self.assertEqual({r["due_date"]: r["amount"] for r in periods(payment["id"])}[TODAY], Decimal("1.00"))

    def test_currency_change_moves_future_periods_to_new_currency(self):
        payment = make_payment()
        edit(payment["id"], currency="EUR", amount=Decimal("950"))
        by_date = {r["due_date"]: r for r in periods(payment["id"])}
        self.assertEqual(by_date[date(2026, 11, 20)]["currency"], "EUR")
        self.assertEqual(by_date[date(2026, 10, 20)]["currency"], "RUB")
        summary = {row["currency"]: row for row in services.summary_by_currency()}
        self.assertEqual(summary["EUR"]["total_amount"], Decimal("950.00"))
        self.assertEqual(summary["RUB"]["total_amount"], Decimal("180000.00"))

    def test_amount_change_resets_reminder_for_new_text(self):
        payment = make_payment(start_date=date(2026, 10, 26))  # через 2 дня — в окне напоминания
        occurrence_id = periods(payment["id"])[0]["id"]
        sqlite_double.execute(
            "UPDATE payment_occurrences SET reminded_at = NOW(), telegram_message_id = 777 WHERE id = %s",
            (occurrence_id,),
        )
        self.assertFalse(services.get_occurrence(occurrence_id)["needs_telegram_reminder"])
        edit(payment["id"], amount=Decimal("95000"))
        occurrence = services.get_occurrence(occurrence_id)
        self.assertTrue(occurrence["needs_telegram_reminder"])
        self.assertIsNone(occurrence["telegram_message_id"])


class EditSchedule(BaseCase):
    def test_anchor_day_change_rebuilds_future_periods(self):
        payment = make_payment()
        updated = edit(payment["id"], anchor_day=5)
        self.assertEqual(updated["anchor_day"], 5)
        self.assertEqual(updated["removed_periods"], 1)
        self.assertEqual(updated["created_periods"], 1)
        self.assertEqual(due_dates(payment["id"]), [date(2026, 9, 20), date(2026, 10, 20), date(2026, 11, 5)])
        self.assertEqual(updated["start_date"], date(2026, 9, 20))  # дата начала не тронута
        self.assertEqual(updated["next_due_date"], date(2026, 9, 20))  # ближайший неоплаченный — старый долг

    def test_anchor_31_goes_to_last_day_and_does_not_drift(self):
        payment = make_payment()
        edit(payment["id"], anchor_day=31)
        self.assertEqual(due_dates(payment["id"])[-1], date(2026, 11, 30))
        services.set_override_today(date(2026, 12, 1))
        self.assertEqual(due_dates(payment["id"])[-1], date(2026, 12, 31))

    def test_anchor_change_never_creates_second_period_in_paid_month(self):
        payment = make_payment(start_date=date(2026, 10, 26))
        mark_paid(periods(payment["id"])[0]["id"])  # оплатили заранее
        edit(payment["id"], anchor_day=15)
        self.assertEqual(due_dates(payment["id"]), [date(2026, 10, 26), date(2026, 11, 15)])

    def test_start_date_change_moves_nearest_payment(self):
        payment = make_payment(start_date=date(2026, 10, 26))
        updated = edit(payment["id"], start_date=date(2026, 11, 10))
        self.assertEqual(updated["start_date"], date(2026, 11, 10))
        self.assertEqual(updated["anchor_day"], 10)  # день взят из новой даты
        self.assertEqual(due_dates(payment["id"]), [date(2026, 11, 10)])
        self.assertEqual(sorted(updated["changed_fields"]), ["anchor_day", "start_date"])

    def test_resending_whole_form_with_same_date_keeps_schedule(self):
        """Форма прислала все поля как есть: anchor_day=31 не должен смениться на день из start_date."""
        payment = make_payment(start_date=date(2026, 9, 30), anchor_day=31)
        before = due_dates(payment["id"])
        updated = edit(payment["id"], name="Аренда офиса", amount=Decimal("90000"), currency="RUB",
                       start_date=date(2026, 9, 30), remind_days_before=3, periodicity="monthly")
        self.assertEqual(updated["changed_fields"], [])
        self.assertEqual(updated["anchor_day"], 31)
        self.assertEqual(due_dates(payment["id"]), before)

    def test_start_date_with_explicit_anchor(self):
        payment = make_payment(start_date=date(2026, 10, 26))
        edit(payment["id"], start_date=date(2026, 11, 30), anchor_day=31)
        services.set_override_today(date(2026, 12, 1))
        self.assertEqual(due_dates(payment["id"]), [date(2026, 11, 30), date(2026, 12, 31)])

    def test_start_date_after_history(self):
        payment = make_payment()
        edit(payment["id"], start_date=date(2026, 11, 28))
        self.assertEqual(due_dates(payment["id"]), [date(2026, 9, 20), date(2026, 10, 20), date(2026, 11, 28)])

    def test_start_date_inside_history_is_rejected_and_nothing_changes(self):
        payment = make_payment()
        before = periods(payment["id"])
        with self.assertRaises(services.ConflictError):
            edit(payment["id"], name="Новое имя", start_date=date(2026, 10, 28))
        # Транзакция откатилась целиком: ни имя, ни периоды не изменились.
        self.assertEqual(services.get_recurring_payment(payment["id"])["name"], "Аренда офиса")
        self.assertEqual([r["id"] for r in periods(payment["id"])], [r["id"] for r in before])

    def test_start_date_in_past_for_new_payment_creates_history(self):
        payment = make_payment(start_date=date(2026, 11, 5))
        edit(payment["id"], start_date=date(2026, 9, 5))
        self.assertEqual(due_dates(payment["id"]), [date(2026, 9, 5), date(2026, 10, 5), date(2026, 11, 5)])

    def test_schedule_and_amount_together(self):
        payment = make_payment()
        updated = edit(payment["id"], anchor_day=1, amount=Decimal("120000"))
        by_date = {r["due_date"]: r["amount"] for r in periods(payment["id"])}
        self.assertEqual(by_date, {
            date(2026, 9, 20): Decimal("90000.00"),
            date(2026, 10, 20): Decimal("90000.00"),
            date(2026, 11, 1): Decimal("120000.00"),
        })
        self.assertEqual(updated["amount"], Decimal("120000.00"))

    def test_paid_period_and_who_paid_survive_schedule_change(self):
        payment = make_payment()
        first = periods(payment["id"])[0]
        mark_paid(first["id"])
        edit(payment["id"], anchor_day=3)
        survived = services.get_occurrence(first["id"])
        self.assertTrue(survived["is_paid"])
        self.assertEqual(survived["paid_by_username"], "ivan")
        self.assertEqual(survived["due_date"], date(2026, 9, 20))


class EditRules(BaseCase):
    def test_deleted_payment_cannot_be_edited(self):
        payment = make_payment()
        services.delete_recurring_payment(payment["id"])
        with self.assertRaises(services.ConflictError):
            edit(payment["id"], name="x")

    def test_missing_payment_is_404(self):
        with self.assertRaises(services.NotFoundError):
            edit(999, name="x")

    def test_schema_rejects_bad_input(self):
        bad_inputs = [
            {},                                   # пусто
            {"name": None},                       # null
            {"amount": None},
            {"name": "   "},                      # пустое имя
            {"amount": 0},                        # сумма не больше нуля
            {"amount": "10.555"},                 # больше двух знаков
            {"currency": "GBP"},                  # чужая валюта
            {"remind_days_before": 2},            # меньше 3 дней
            {"anchor_day": 32},
            {"periodicity": "weekly"},
            {"is_deleted": False},                # такого поля нет
        ]
        for data in bad_inputs:
            with self.subTest(data=data), self.assertRaises(ValidationError):
                RecurringPaymentUpdate(**data)


class PermanentDelete(BaseCase):
    def test_removes_payment_and_all_periods(self):
        payment = make_payment()
        mark_paid(periods(payment["id"])[0]["id"])
        snapshot, removed = services.delete_recurring_payment_permanently(payment["id"])
        self.assertEqual(snapshot["name"], "Аренда офиса")
        self.assertEqual(removed, 3)
        self.assertEqual(due_dates(payment["id"]), [])
        with self.assertRaises(services.NotFoundError):
            services.get_recurring_payment(payment["id"])
        self.assertEqual(services.list_recurring_payments(include_deleted=True), [])

    def test_soft_deleted_payment_can_be_removed_completely(self):
        payment = make_payment()
        services.delete_recurring_payment(payment["id"])
        _, removed = services.delete_recurring_payment_permanently(payment["id"])
        self.assertEqual(removed, 3)

    def test_other_payments_are_untouched(self):
        first = make_payment()
        second = make_payment(name="Сервер", currency="USD", amount=Decimal("165"))
        services.delete_recurring_payment_permanently(first["id"])
        self.assertEqual(len(due_dates(second["id"])), 3)

    def test_missing_payment_is_404(self):
        with self.assertRaises(services.NotFoundError):
            services.delete_recurring_payment_permanently(999)


if __name__ == "__main__":
    unittest.main()
