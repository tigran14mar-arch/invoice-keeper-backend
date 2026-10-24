"""
Напоминания в Telegram и фоновый планировщик.

Что здесь происходит:
 1. Планировщик раз в N секунд достраивает периоды всех активных платежей
    (следующий период появляется даже если предыдущий не оплачен) и проверяет сроки.
 2. Период попадает в напоминание, если шаблон не удалён, период не оплачен,
    напоминание по нему ещё не отправлялось и до срока осталось не больше
    remind_days_before дней. Это правило живёт в представлении базы
    v_payment_occurrences_detailed (поле needs_telegram_reminder).
 3. Защита от дублей: перед отправкой период «бронируется» одним UPDATE
    (reminded_at ставится только если он был NULL). Если отправка не удалась,
    бронь снимается и попытка повторится в следующем цикле.
 4. Бонус: к сообщению прикрепляется кнопка «Оплачено», нажатие приходит
    через getUpdates и отмечает период оплаченным.

HTTP-запросы к Telegram сделаны на стандартной библиотеке, чтобы не тянуть
лишних зависимостей; блокирующие вызовы уходят в отдельный поток.
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from decimal import Decimal
from typing import Any, Optional

from app import services
from app.config import settings
from app.database import execute, query_all, query_one, today as db_today
from app.security import log_action

log = logging.getLogger("invoice_keeper.reminders")

API_BASE = "https://api.telegram.org"
REQUEST_TIMEOUT = 15
POLL_TIMEOUT = 25
CALLBACK_PREFIX = "paid:"


class TelegramError(RuntimeError):
    """Ошибка обращения к Telegram. Текст никогда не содержит токен бота."""


# =============================================================================
# Клиент Telegram
# =============================================================================

def _call(method: str, payload: dict, timeout: int = REQUEST_TIMEOUT) -> Any:
    if not settings.telegram_bot_token:
        raise TelegramError("TELEGRAM_BOT_TOKEN не задан в .env")
    url = f"{API_BASE}/bot{settings.telegram_bot_token}/{method}"
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        description = ""
        try:
            description = json.load(exc).get("description", "")
        except Exception:  # noqa: BLE001
            pass
        raise TelegramError(f"Telegram вернул HTTP {exc.code}: {description}".strip(": ")) from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", None) or type(exc).__name__
        raise TelegramError(f"Не удалось связаться с Telegram: {reason}") from None
    if not body.get("ok"):
        raise TelegramError(f"Telegram отклонил запрос: {body.get('description', 'неизвестная ошибка')}")
    return body.get("result")


def send_message(text: str, *, occurrence_id: Optional[int] = None, chat_id: Optional[str] = None) -> Optional[int]:
    """Отправляет сообщение в чат. Возвращает message_id."""
    payload: dict[str, Any] = {
        "chat_id": chat_id or settings.telegram_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if occurrence_id is not None and settings.telegram_paid_button:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": "✅ Оплачено", "callback_data": f"{CALLBACK_PREFIX}{occurrence_id}"}]]
        }
    result = _call("sendMessage", payload)
    return result.get("message_id") if isinstance(result, dict) else None


def answer_callback(callback_id: str, text: str) -> None:
    _call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text, "show_alert": False})


def edit_message(chat_id: Any, message_id: int, text: str) -> None:
    _call("editMessageText", {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": []},
    })


def get_updates(offset: int) -> list[dict]:
    result = _call(
        "getUpdates",
        {"offset": offset, "timeout": POLL_TIMEOUT, "allowed_updates": ["callback_query"]},
        timeout=POLL_TIMEOUT + 10,
    )
    return result or []


# =============================================================================
# Текст сообщений
# =============================================================================

def _format_amount(amount: Decimal, currency: str) -> str:
    formatted = f"{Decimal(amount):,.2f}".replace(",", " ")
    return f"{formatted} {currency}"


def _days_phrase(due: date, today_: date) -> str:
    left = (due - today_).days
    if left > 1:
        return f"через {left} дн."
    if left == 1:
        return "завтра"
    if left == 0:
        return "сегодня"
    return f"просрочено на {-left} дн."


def build_reminder_text(occurrence: dict, today_: date) -> str:
    """Название, сумма, валюта и дата оплаты — обязательный состав напоминания (Слайд 7)."""
    due = occurrence["due_date"]
    return (
        "🔔 <b>Напоминание об оплате</b>\n"
        f"<b>{_escape(occurrence['name'])}</b>\n"
        f"Сумма: {_format_amount(occurrence['amount'], occurrence['currency'])}\n"
        f"Срок оплаты: {due.strftime('%d.%m.%Y')} ({_days_phrase(due, today_)})"
    )


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# =============================================================================
# Цикл напоминаний
# =============================================================================

def _claim(occurrence_id: int) -> bool:
    """Бронирует период под отправку. Выигрывает ровно один вызов — дублей не будет."""
    rowcount, _ = execute(
        "UPDATE payment_occurrences SET reminded_at = NOW() WHERE id = %s AND reminded_at IS NULL",
        (occurrence_id,),
    )
    return rowcount == 1


def _release(occurrence_id: int) -> None:
    execute("UPDATE payment_occurrences SET reminded_at = NULL WHERE id = %s", (occurrence_id,))


def run_reminder_cycle() -> dict:
    """Один проход планировщика. Безопасно вызывать вручную (POST /api/v1/reminders/run)."""
    created = services.ensure_all_periods()
    today_ = db_today()
    candidates = query_all(
        "SELECT id, recurring_payment_id, name, amount, currency, due_date "
        "FROM v_payment_occurrences_detailed WHERE needs_telegram_reminder = TRUE ORDER BY due_date, id"
    )
    result = {
        "checked": len(candidates),
        "sent": 0,
        "failed": 0,
        "created_periods": created,
        "telegram_enabled": settings.telegram_enabled,
    }
    if not settings.telegram_enabled:
        if candidates:
            log.warning(
                "Telegram не настроен: %s напоминаний не отправлено. "
                "Заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env",
                len(candidates),
            )
        return result

    for occurrence in candidates:
        if not _claim(occurrence["id"]):
            continue  # уже забрал другой цикл или период успели оплатить
        try:
            message_id = send_message(
                build_reminder_text(occurrence, today_), occurrence_id=occurrence["id"]
            )
        except TelegramError as exc:
            _release(occurrence["id"])
            result["failed"] += 1
            log.error("Напоминание по периоду %s не отправлено: %s", occurrence["id"], exc)
            continue
        if message_id is not None:
            execute(
                "UPDATE payment_occurrences SET telegram_message_id = %s WHERE id = %s",
                (message_id, occurrence["id"]),
            )
        result["sent"] += 1
        log_action(
            "system", "admin", "TELEGRAM_REMINDER_SENT",
            entity_type="payment_occurrence", entity_id=occurrence["id"],
            details={"name": occurrence["name"], "due_date": str(occurrence["due_date"])},
        )
        log.info("Напоминание отправлено: период %s (%s)", occurrence["id"], occurrence["name"])
    return result


# =============================================================================
# Кнопка «Оплачено» (бонус, Слайд 16)
# =============================================================================

def handle_callback(update: dict) -> Optional[str]:
    """Обрабатывает нажатие кнопки под сообщением бота."""
    callback = update.get("callback_query")
    if not callback:
        return None
    data = callback.get("data") or ""
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")

    if not data.startswith(CALLBACK_PREFIX):
        answer_callback(callback["id"], "Неизвестная команда")
        return "ignored"
    if settings.telegram_chat_id and str(chat_id) != str(settings.telegram_chat_id):
        answer_callback(callback["id"], "Это действие недоступно в данном чате")
        return "foreign_chat"

    try:
        occurrence_id = int(data[len(CALLBACK_PREFIX):])
    except ValueError:
        answer_callback(callback["id"], "Платёж не найден")
        return "bad_data"

    sender = callback.get("from") or {}
    linked = query_one("SELECT username FROM users WHERE telegram_user_id = %s", (sender.get("id"),))
    actor = linked["username"] if linked else f"telegram:{sender.get('username') or sender.get('id')}"

    occurrence = services.get_occurrence_safe(occurrence_id)
    if occurrence is None:
        answer_callback(callback["id"], "Платёж не найден")
        return "not_found"
    if occurrence["is_paid"]:
        answer_callback(callback["id"], "Этот период уже отмечен оплаченным")
        return "already_paid"

    updated = services.mark_paid_from_telegram(occurrence_id, actor)
    if updated is None:
        answer_callback(callback["id"], "Платёж не найден")
        return "not_found"

    answer_callback(callback["id"], "Отмечено как оплачено ✅")
    log_action(
        actor, "user", "MARK_PAID",
        entity_type="payment_occurrence", entity_id=occurrence_id,
        details={"source": "telegram_button", "actual_payment_date": str(updated["actual_payment_date"])},
    )
    if chat_id and message.get("message_id"):
        try:
            edit_message(
                chat_id,
                message["message_id"],
                f"✅ <b>Оплачено</b>\n<b>{_escape(updated['name'])}</b>\n"
                f"Сумма: {_format_amount(updated['amount'], updated['currency'])}\n"
                f"Срок оплаты: {updated['due_date'].strftime('%d.%m.%Y')}\n"
                f"Дата оплаты: {updated['actual_payment_date'].strftime('%d.%m.%Y')} · отметил {_escape(actor)}",
            )
        except TelegramError as exc:
            log.warning("Не удалось обновить сообщение в Telegram: %s", exc)
    return "paid"


# =============================================================================
# Фоновые задачи приложения
# =============================================================================

async def scheduler_task() -> None:
    """Периодическая проверка сроков. Работает, даже если Telegram не настроен:
    периоды всё равно должны достраиваться."""
    interval = max(10, settings.scheduler_interval_seconds)
    log.info("Планировщик запущен: проверка каждые %s с", interval)
    while True:
        try:
            await asyncio.to_thread(run_reminder_cycle)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — цикл не должен умирать из-за одной ошибки
            log.exception("Ошибка цикла напоминаний")
        await asyncio.sleep(interval)


async def callback_task() -> None:
    """Приём нажатий кнопки «Оплачено»."""
    if not settings.telegram_enabled or not settings.telegram_paid_button:
        return
    log.info("Приём нажатий кнопки «Оплачено» запущен")
    offset = 0
    delay = 2
    while True:
        try:
            updates = await asyncio.to_thread(get_updates, offset)
            delay = 2
            for update in updates:
                offset = max(offset, update["update_id"] + 1)
                try:
                    await asyncio.to_thread(handle_callback, update)
                except TelegramError as exc:
                    log.warning("Ошибка обработки нажатия: %s", exc)
                except Exception:  # noqa: BLE001
                    log.exception("Сбой обработки обновления Telegram")
        except asyncio.CancelledError:
            raise
        except TelegramError as exc:
            log.warning("Telegram недоступен: %s (повтор через %s с)", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
        except Exception:  # noqa: BLE001
            log.exception("Сбой приёма обновлений Telegram")
            await asyncio.sleep(10)
