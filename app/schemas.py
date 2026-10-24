"""
Схемы запросов и ответов (Pydantic v2).

Это контракт API: что приходит от клиента и что уходит обратно.
Валидация здесь — первый рубеж защиты правил ТЗ (валюты, суммы, даты, роли).
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Currency = Literal["RUB", "USD", "EUR"]
Role = Literal["admin", "user"]
Periodicity = Literal["monthly"]

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
MAX_AMOUNT = Decimal("9999999999999.99")


def _check_password(value: str) -> str:
    # Те же правила, что на странице регистрации интерфейса: 8+ символов и хотя бы одна буква.
    if len(value) < 8:
        raise ValueError("Пароль должен содержать не менее 8 символов")
    if not re.search(r"[^\W\d_]", value, re.UNICODE):
        raise ValueError("Пароль должен содержать хотя бы одну букву")
    if len(value.encode("utf-8")) > 72:
        raise ValueError("Пароль слишком длинный (максимум 72 байта)")
    return value


def _check_username(value: str) -> str:
    value = value.strip()
    if not USERNAME_RE.match(value):
        raise ValueError("Логин: 3–32 символа, латинские буквы, цифры и символы . _ -")
    return value


def _check_amount(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("Сумма должна быть числом")
    if value <= 0:
        raise ValueError("Сумма должна быть больше нуля")
    if value > MAX_AMOUNT:
        raise ValueError("Сумма слишком большая")
    if value.as_tuple().exponent < -2:
        raise ValueError("Сумма указывается с точностью до двух знаков после запятой")
    return value


# --------------------------------------------------------------------------- общее

class Message(BaseModel):
    detail: str = Field(examples=["Готово"])


class ErrorResponse(BaseModel):
    detail: str = Field(examples=["Недостаточно прав: действие доступно только администратору"])


# --------------------------------------------------------------------------- авторизация

class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=100, examples=["admin"])
    password: str = Field(min_length=1, max_length=200, examples=["admin12345"])


class UserOut(BaseModel):
    id: Optional[int] = Field(default=None, description="ID в таблице users")
    username: str
    full_name: Optional[str] = None
    role: Role
    is_env_admin: bool = False
    is_active: bool = True
    telegram_user_id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class LoginResponse(BaseModel):
    access_token: str = Field(description="Токен сессии. Дублируется в HttpOnly-cookie.")
    token_type: Literal["bearer"] = "bearer"
    expires_at: datetime
    user: UserOut


# --------------------------------------------------------------------------- пользователи

class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(examples=["ivan_accountant"])
    password: str = Field(examples=["ivanpass123"])
    full_name: Optional[str] = Field(default=None, max_length=150, examples=["Иван (Бухгалтер)"])
    telegram_user_id: Optional[int] = Field(default=None, description="Telegram ID для кнопки «Оплачено» в боте")
    is_active: bool = True

    @field_validator("username")
    @classmethod
    def _username(cls, value: str) -> str:
        return _check_username(value)

    @field_validator("password")
    @classmethod
    def _password(cls, value: str) -> str:
        return _check_password(value)


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: Optional[str] = None
    password: Optional[str] = None
    full_name: Optional[str] = Field(default=None, max_length=150)
    telegram_user_id: Optional[int] = None
    is_active: Optional[bool] = None

    @field_validator("username")
    @classmethod
    def _username(cls, value: Optional[str]) -> Optional[str]:
        return _check_username(value) if value is not None else None

    @field_validator("password")
    @classmethod
    def _password(cls, value: Optional[str]) -> Optional[str]:
        return _check_password(value) if value is not None else None

    @model_validator(mode="after")
    def _not_empty(self):
        if not self.model_dump(exclude_unset=True):
            raise ValueError("Передайте хотя бы одно поле для изменения")
        return self


# --------------------------------------------------------------------------- регулярные платежи

class RecurringPaymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255, examples=["Аренда офиса"])
    amount: Decimal = Field(examples=[90000.00])
    currency: Currency = Field(examples=["RUB"])
    start_date: date = Field(description="Дата ближайшей оплаты — она же первый период", examples=["2026-11-05"])
    periodicity: Periodicity = "monthly"
    anchor_day: Optional[int] = Field(
        default=None, ge=1, le=31,
        description="День месяца для следующих периодов. По умолчанию — день из start_date. "
                    "Если в месяце нет такого числа, срок переносится на последний день месяца.",
    )
    remind_days_before: int = Field(
        default=3, ge=3, le=60,
        description="За сколько дней напоминать в Telegram. Минимум 3 (требование кейса).",
    )

    @field_validator("amount")
    @classmethod
    def _amount(cls, value: Decimal) -> Decimal:
        return _check_amount(value)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Название не может быть пустым")
        return value

    @field_validator("start_date")
    @classmethod
    def _start_date(cls, value: date) -> date:
        if value.year < 2000 or value.year > 2100:
            raise ValueError("Дата ближайшей оплаты вне допустимого диапазона")
        return value


class RecurringPaymentUpdate(BaseModel):
    """PATCH: изменение регулярного платежа администратором.

    Передаются только те поля, которые меняются. История (оплаченные и прошедшие
    периоды) не переписывается — меняются шаблон и ещё не наступившие неоплаченные периоды.
    """
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(default=None, min_length=1, max_length=255, examples=["Аренда офиса (новый БЦ)"])
    amount: Optional[Decimal] = Field(default=None, examples=[95000.00])
    currency: Optional[Currency] = Field(default=None, examples=["RUB"])
    start_date: Optional[date] = Field(
        default=None,
        description="Новая дата ближайшей оплаты. Будущие неоплаченные периоды пересоздаются от неё.",
        examples=["2026-11-10"],
    )
    periodicity: Optional[Periodicity] = None
    anchor_day: Optional[int] = Field(
        default=None, ge=1, le=31,
        description="Новый день месяца для периодов. Если передан только start_date — берётся день из него.",
    )
    remind_days_before: Optional[int] = Field(default=None, ge=3, le=60)

    @field_validator("amount")
    @classmethod
    def _amount(cls, value: Optional[Decimal]) -> Optional[Decimal]:
        return _check_amount(value) if value is not None else None

    @field_validator("name")
    @classmethod
    def _name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Название не может быть пустым")
        return value

    @field_validator("start_date")
    @classmethod
    def _start_date(cls, value: Optional[date]) -> Optional[date]:
        if value is not None and (value.year < 2000 or value.year > 2100):
            raise ValueError("Дата ближайшей оплаты вне допустимого диапазона")
        return value

    @model_validator(mode="after")
    def _rules(self):
        fields = self.model_dump(exclude_unset=True)
        if not fields:
            raise ValueError("Передайте хотя бы одно поле для изменения")
        empty = sorted(key for key, value in fields.items() if value is None)
        if empty:
            raise ValueError(f"Поля не могут быть null: {', '.join(empty)}")
        return self


class RecurringPaymentOut(BaseModel):
    id: int
    name: str
    amount: Decimal
    currency: Currency
    periodicity: Periodicity
    start_date: date
    anchor_day: int
    remind_days_before: int
    is_deleted: bool
    deleted_at: Optional[datetime] = None
    created_by: str
    created_at: datetime
    updated_at: datetime
    next_due_date: Optional[date] = Field(default=None, description="Срок ближайшего неоплаченного периода")
    periods_total: int = 0
    periods_unpaid: int = 0


class RecurringPaymentEditOut(RecurringPaymentOut):
    """Ответ на PATCH: платёж после изменения и что произошло с его периодами."""
    changed_fields: list[str] = Field(default_factory=list, description="Какие поля реально изменились")
    updated_periods: int = Field(default=0, description="Сколько будущих периодов получили новую сумму/валюту")
    removed_periods: int = Field(default=0, description="Сколько будущих неоплаченных периодов удалено при смене графика")
    created_periods: int = Field(default=0, description="Сколько периодов создано по новому графику")


class RecurringPaymentDeleteOut(RecurringPaymentOut):
    """Ответ на DELETE: платёж в момент удаления."""
    permanent: bool = Field(default=False, description="true — удалён полностью вместе с историей")
    removed_periods: int = Field(default=0, description="Сколько периодов удалено (только при permanent=true)")


# --------------------------------------------------------------------------- периоды оплаты

class PaymentOccurrenceOut(BaseModel):
    id: int
    recurring_payment_id: int
    name: str
    amount: Decimal
    currency: Currency
    periodicity: Periodicity
    anchor_day: int
    is_recurring_deleted: bool
    due_date: date
    is_paid: bool
    status: Literal["paid", "unpaid"]
    actual_payment_date: Optional[date] = None
    is_overdue: bool
    overdue_days: int
    needs_telegram_reminder: bool
    paid_by_username: Optional[str] = None
    reminded_at: Optional[datetime] = None
    telegram_message_id: Optional[int] = None
    receipt_url: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class OccurrenceListOut(BaseModel):
    total: int = Field(description="Сколько всего записей подходит под фильтры")
    limit: int
    offset: int
    items: list[PaymentOccurrenceOut]


class PaymentOccurrenceUpdate(BaseModel):
    """PATCH: подтверждение или отмена оплаты и исправление фактической даты.

    Плановый срок (due_date) не меняется никогда — это правило ТЗ и оно
    дополнительно закреплено триггером в базе.
    """
    model_config = ConfigDict(extra="forbid")
    is_paid: Optional[bool] = Field(default=None, description="true — оплачено, false — снять отметку")
    actual_payment_date: Optional[date] = Field(default=None, description="Фактическая дата оплаты")
    receipt_url: Optional[str] = Field(default=None, max_length=500, description="Ссылка на чек")
    notes: Optional[str] = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _rules(self):
        fields = self.model_dump(exclude_unset=True)
        if not fields:
            raise ValueError("Передайте хотя бы одно поле: is_paid, actual_payment_date, receipt_url или notes")
        if self.is_paid is False and fields.get("actual_payment_date") is not None:
            raise ValueError("Для неоплаченного периода фактическая дата не указывается — она очищается")
        return self


# --------------------------------------------------------------------------- итоги по валютам

class CurrencySummary(BaseModel):
    currency: Currency
    total_periods_count: int
    total_amount: Decimal
    paid_amount: Decimal
    unpaid_amount: Decimal
    active_overdue_amount: Decimal = Field(description="Сумма неоплаченных просроченных периодов")
    paid_with_overdue_amount: Decimal = Field(description="Сумма оплаченных с опозданием периодов")
    active_overdue_count: int
    unpaid_count: int
    paid_count: int


class SummaryOut(BaseModel):
    today: date
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    currencies: list[CurrencySummary] = Field(description="Всегда три валюты: RUB, USD, EUR — без конвертации")


# --------------------------------------------------------------------------- календарь и дашборд

class CalendarDay(BaseModel):
    day: date
    is_today: bool
    total_count: int
    paid_count: int
    unpaid_count: int
    overdue_count: int
    items: list[PaymentOccurrenceOut]


class CalendarOut(BaseModel):
    year: int
    month: int
    today: date
    days: list[CalendarDay]


class DashboardCounts(BaseModel):
    overdue: int
    due_soon: int
    unpaid: int
    paid: int
    active_recurring: int


class DashboardOut(BaseModel):
    today: date
    timezone: str
    is_demo_date: bool
    user: UserOut
    counts: DashboardCounts
    month_summary: list[CurrencySummary] = Field(description="Итоги за текущий месяц по трём валютам")
    total_summary: list[CurrencySummary] = Field(description="Итоги за всё время по трём валютам")
    overdue: list[PaymentOccurrenceOut]
    upcoming: list[PaymentOccurrenceOut]
    recently_paid: list[PaymentOccurrenceOut]


# --------------------------------------------------------------------------- настройки и демо-режим

class SettingsOut(BaseModel):
    timezone: str
    today: date
    override_today: Optional[date] = Field(default=None, description="Демо-сдвиг «сегодня»; null — реальная дата")
    real_today: date
    telegram_enabled: bool
    scheduler_enabled: bool
    default_remind_days_before: int


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    override_today: Optional[date] = Field(
        default=None, description="Дата для живой демонстрации. null — вернуться к реальной дате.",
    )


# --------------------------------------------------------------------------- бюджеты (бонус)

class BudgetUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")
    period_type: Literal["monthly", "yearly"]
    period_year: int = Field(ge=2000, le=2100)
    period_month: int = Field(default=0, ge=0, le=12, description="1–12 для monthly, 0 для yearly")
    currency: Currency
    planned_limit: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def _month(self):
        if self.period_type == "monthly" and not 1 <= self.period_month <= 12:
            raise ValueError("Для месячного бюджета period_month должен быть от 1 до 12")
        if self.period_type == "yearly" and self.period_month != 0:
            raise ValueError("Для годового бюджета period_month должен быть 0")
        return self


class BudgetOut(BaseModel):
    id: int
    period_type: Literal["monthly", "yearly"]
    period_year: int
    period_month: int
    currency: Currency
    planned_limit: Decimal
    spent_amount: Decimal = Field(default=Decimal("0.00"), description="Сумма оплаченных периодов в этом бюджете")
    planned_payments_amount: Decimal = Field(default=Decimal("0.00"), description="Сумма всех периодов в этом бюджете")
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------- журнал действий (бонус)

class AuditLogOut(BaseModel):
    id: int
    actor_username: str
    actor_role: Role
    action_type: str
    entity_type: Optional[str] = None
    entity_id: Optional[int] = None
    details: Optional[dict] = None
    ip_address: Optional[str] = None
    created_at: datetime


class ReminderRunOut(BaseModel):
    checked: int = Field(description="Сколько периодов подошло под условие напоминания")
    sent: int
    failed: int
    created_periods: int = Field(description="Сколько новых периодов создано при проверке")
    telegram_enabled: bool
