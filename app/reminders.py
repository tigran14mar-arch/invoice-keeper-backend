"""
Напоминания в Telegram и фоновый планировщик.

Групповой бот: напоминания рассылаются в общий чат (TELEGRAM_CHAT_ID из .env)
и во все привязанные группы пользователей. При отметке оплаты сообщения
обновляются во всех чатах.

Отслеживание сообщений ведётся в таблице telegram_messages (occurrence_id,
chat_id, message_id), что позволяет закрывать напоминания везде.

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
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Optional

from app import services
from app.config import settings
from app.database import execute, query_all, query_one, today as db_today, transaction
from app.security import log_action

log = logging.getLogger("invoice_keeper.reminders")

API_BASE = "https://api.telegram.org"
REQUEST_TIMEOUT = 15
POLL_TIMEOUT = 25
CALLBACK_PREFIX = "paid:"
DEFAULT_CHAT = "default"


# =============================================================================
# Ошибка и клиент Telegram
# =============================================================================

class TelegramError(RuntimeError):
    """Ошибка обращения к Telegram. Текст никогда не содержит токен бота."""

    def __init__(self, message: str, *, error_code: int = 0, parameters: Optional[dict] = None):
        super().__init__(message)
        self.error_code = error_code
        self.parameters = parameters or {}

    @property
    def migrate_to_chat_id(self) -> Optional[int]:
        return self.parameters.get("migrate_to_chat_id")

    @property
    def chat_is_gone(self) -> bool:
        if self.error_code == 403:
            return True
        if self.error_code == 400 and self.migrate_to_chat_id:
            return True
        return False


class TelegramClient:
    """HTTP-клиент для Telegram Bot API на стандартной библиотеке."""

    def __init__(self, token: str, common_chat_id: str = ""):
        self._token = token
        self.chat_id = common_chat_id  # общий чат из .env
        self._bot_username: Optional[str] = None

    def _call(self, method: str, payload: dict, timeout: int = REQUEST_TIMEOUT) -> Any:
        url = f"{API_BASE}/bot{self._token}/{method}"
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            error_body: dict = {}
            try:
                error_body = json.load(exc)
            except Exception:  # noqa: BLE001
                pass
            description = error_body.get("description", "")
            parameters = error_body.get("parameters", {})
            raise TelegramError(
                f"Telegram HTTP {exc.code}: {description}".strip(": "),
                error_code=exc.code,
                parameters=parameters,
            ) from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", None) or type(exc).__name__
            raise TelegramError(f"Не удалось связаться с Telegram: {reason}") from None
        if not body.get("ok"):
            raise TelegramError(
                f"Telegram отклонил запрос: {body.get('description', 'неизвестная ошибка')}"
            )
        return body.get("result")

    def bot_username(self) -> str:
        if self._bot_username is None:
            me = self._call("getMe", {})
            self._bot_username = me.get("username", "")
        return self._bot_username

    def send(
        self,
        text: str,
        *,
        chat_id: Optional[str] = None,
        occurrence_id: Optional[int] = None,
        reply_markup: Optional[dict] = None,
    ) -> Optional[int]:
        """Отправить сообщение. Возвращает message_id."""
        target = chat_id or self.chat_id
        if not target:
            raise TelegramError("Не задан chat_id для отправки")
        payload: dict[str, Any] = {
            "chat_id": target,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        elif occurrence_id is not None and settings.telegram_paid_button:
            payload["reply_markup"] = {
                "inline_keyboard": [[{
                    "text": "✅ Оплачено",
                    "callback_data": f"{CALLBACK_PREFIX}{occurrence_id}",
                }]]
            }
        result = self._call("sendMessage", payload)
        return result.get("message_id") if isinstance(result, dict) else None

    def answer_callback(self, callback_id: str, text: str) -> None:
        self._call(
            "answerCallbackQuery",
            {"callback_query_id": callback_id, "text": text, "show_alert": False},
        )

    def finish_message(self, chat_id: Any, message_id: int, text: str) -> None:
        """Обновляет сообщение — убирает кнопку и ставит финальный текст."""
        self._call("editMessageText", {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": []},
        })

    def get_updates(self, offset: int) -> list[dict]:
        result = self._call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": POLL_TIMEOUT,
                "allowed_updates": ["callback_query", "message", "my_chat_member"],
            },
            timeout=POLL_TIMEOUT + 10,
        )
        return result or []

    def is_chat_admin(self, chat_id: Any, user_id: int) -> bool:
        try:
            member = self._call("getChatMember", {"chat_id": chat_id, "user_id": user_id})
            return member.get("status") in ("creator", "administrator")
        except TelegramError:
            return False


class DisabledClient:
    """Заглушка, если Telegram не настроен."""
    chat_id = ""

    def bot_username(self) -> str:
        return ""

    def send(self, *_a, **_kw) -> None:
        return None

    def answer_callback(self, *_a, **_kw) -> None:
        pass

    def finish_message(self, *_a, **_kw) -> None:
        pass

    def get_updates(self, _offset: int) -> list:
        return []

    def is_chat_admin(self, *_a, **_kw) -> bool:
        return False


def build_client() -> TelegramClient | DisabledClient:
    if settings.telegram_bot_token:
        return TelegramClient(settings.telegram_bot_token, settings.telegram_chat_id)
    return DisabledClient()


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


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_reminder_text(occurrence: dict, today_: date) -> str:
    """Название, сумма, валюта и дата оплаты — обязательный состав напоминания."""
    due = occurrence["due_date"]
    return (
        "🔔 <b>Напоминание об оплате</b>\n"
        f"<b>{_escape(occurrence['name'])}</b>\n"
        f"Сумма: {_format_amount(occurrence['amount'], occurrence['currency'])}\n"
        f"Срок оплаты: {due.strftime('%d.%m.%Y')} ({_days_phrase(due, today_)})"
    )


def build_paid_text(occurrence: dict, actor: str) -> str:
    return (
        f"✅ <b>Оплачено</b>\n"
        f"<b>{_escape(occurrence['name'])}</b>\n"
        f"Сумма: {_format_amount(occurrence['amount'], occurrence['currency'])}\n"
        f"Срок оплаты: {occurrence['due_date'].strftime('%d.%m.%Y')}\n"
        f"Дата оплаты: {occurrence['actual_payment_date'].strftime('%d.%m.%Y')}"
        f" · отметил {_escape(actor)}"
    )


# =============================================================================
# Рассылка и отслеживание сообщений
# =============================================================================

def group_chats() -> list[str]:
    """Все привязанные группы (chat_id начинается с '-')."""
    rows = query_all(
        "SELECT telegram_chat_id FROM users WHERE telegram_chat_id IS NOT NULL"
    )
    return [r["telegram_chat_id"] for r in rows if r["telegram_chat_id"].startswith("-")]


def targets(client: TelegramClient | DisabledClient) -> list[Optional[str]]:
    """Общий чат + все группы без дублей. None = общий чат (client.chat_id)."""
    result: list[Optional[str]] = []
    common = client.chat_id
    if common:
        result.append(None)  # None означает «общий чат из .env»
    for chat in group_chats():
        if chat != common:
            result.append(chat)
    return result


def broadcast(
    client: TelegramClient | DisabledClient,
    text: str,
    occurrence_id: int,
) -> list[tuple[str, int]]:
    """Рассылает сообщение во все чаты. Возвращает [(chat_key, message_id), ...]."""
    delivered: list[tuple[str, int]] = []
    for target in targets(client):
        actual_chat_id = target  # None → client.send() использует client.chat_id
        chat_key = target or DEFAULT_CHAT
        try:
            msg_id = client.send(
                text, chat_id=actual_chat_id, occurrence_id=occurrence_id,
            )
            if msg_id is not None:
                delivered.append((chat_key, msg_id))
        except TelegramError as exc:
            if exc.migrate_to_chat_id:
                new_chat = str(exc.migrate_to_chat_id)
                _handle_migration(actual_chat_id, new_chat)
                try:
                    msg_id = client.send(
                        text, chat_id=new_chat, occurrence_id=occurrence_id,
                    )
                    if msg_id is not None:
                        delivered.append((new_chat, msg_id))
                except TelegramError as exc2:
                    log.warning("Не удалось отправить в мигрированный чат %s: %s", new_chat, exc2)
            elif exc.chat_is_gone and actual_chat_id:
                _handle_gone_chat(actual_chat_id)
            else:
                log.warning("Не удалось отправить в чат %s: %s", chat_key, exc)
    return delivered


def remember_messages(occurrence_id: int, delivered: list[tuple[str, int]]) -> None:
    """Сохраняет отправленные сообщения в telegram_messages."""
    for chat_key, message_id in delivered:
        execute(
            "INSERT INTO telegram_messages (occurrence_id, chat_id, message_id) VALUES (%s, %s, %s)",
            (occurrence_id, chat_key, message_id),
        )


def close_reminders(
    client: TelegramClient | DisabledClient,
    occurrence_id: int,
    text: str,
    skip: Optional[str] = None,
) -> None:
    """Обновляет сообщения-напоминания во всех чатах при оплате."""
    rows = query_all(
        "SELECT chat_id, message_id FROM telegram_messages WHERE occurrence_id = %s",
        (occurrence_id,),
    )
    for row in rows:
        chat = row["chat_id"]
        if chat == skip:
            continue
        actual_chat = client.chat_id if chat == DEFAULT_CHAT else chat
        if not actual_chat:
            continue
        try:
            client.finish_message(actual_chat, row["message_id"], text)
        except TelegramError as exc:
            log.warning("Не удалось обновить сообщение %s/%s: %s", chat, row["message_id"], exc)


def _handle_migration(old_chat: Optional[str], new_chat: str) -> None:
    """Обрабатывает миграцию группы в супергруппу."""
    if old_chat:
        from app.services import migrate_chat
        try:
            migrate_chat(old_chat, new_chat)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка миграции чата %s -> %s", old_chat, new_chat)


def _handle_gone_chat(chat_id: str) -> None:
    """Бот удалён из группы — отвязываем."""
    from app.services import unlink_by_chat
    try:
        unlink_by_chat(chat_id)
        log.info("Группа %s отвязана (бот удалён)", chat_id)
    except Exception:  # noqa: BLE001
        log.exception("Ошибка при отвязке чата %s", chat_id)


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


@dataclass
class ReminderResult:
    checked: int = 0
    sent: int = 0
    failed: int = 0
    created_periods: int = 0
    telegram_enabled: bool = False


def run_reminder_cycle(client: Optional[TelegramClient | DisabledClient] = None) -> dict:
    """Один проход планировщика. Безопасно вызывать вручную (POST /api/v1/reminders/run)."""
    if client is None:
        client = build_client()
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
                "Заполните TELEGRAM_BOT_TOKEN в .env",
                len(candidates),
            )
        return result

    for occurrence in candidates:
        if not _claim(occurrence["id"]):
            continue
        text = build_reminder_text(occurrence, today_)
        delivered = broadcast(client, text, occurrence["id"])
        if not delivered:
            _release(occurrence["id"])
            result["failed"] += 1
            log.error("Напоминание по периоду %s не доставлено ни в один чат", occurrence["id"])
            continue
        remember_messages(occurrence["id"], delivered)
        # Обратная совместимость: сохраняем message_id первого сообщения
        first_msg_id = delivered[0][1] if delivered else None
        if first_msg_id is not None:
            execute(
                "UPDATE payment_occurrences SET telegram_message_id = %s WHERE id = %s",
                (first_msg_id, occurrence["id"]),
            )
        result["sent"] += 1
        log_action(
            "system", "admin", "TELEGRAM_REMINDER_SENT",
            entity_type="payment_occurrence", entity_id=occurrence["id"],
            details={"name": occurrence["name"], "due_date": str(occurrence["due_date"]),
                     "chats": len(delivered)},
        )
        log.info(
            "Напоминание отправлено: период %s (%s) в %s чатов",
            occurrence["id"], occurrence["name"], len(delivered),
        )
    return result


# =============================================================================
# Фоновые задачи приложения
# =============================================================================

_client: Optional[TelegramClient | DisabledClient] = None


def get_client() -> TelegramClient | DisabledClient:
    global _client
    if _client is None:
        _client = build_client()
    return _client


async def scheduler_task() -> None:
    """Периодическая проверка сроков. Работает, даже если Telegram не настроен:
    периоды всё равно должны достраиваться."""
    interval = max(10, settings.scheduler_interval_seconds)
    log.info("Планировщик запущен: проверка каждые %s с", interval)
    client = get_client()
    while True:
        try:
            await asyncio.to_thread(run_reminder_cycle, client)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — цикл не должен умирать из-за одной ошибки
            log.exception("Ошибка цикла напоминаний")
        await asyncio.sleep(interval)


async def callback_task() -> None:
    """Приём обновлений Telegram: кнопки «Оплачено», команды бота, события чатов."""
    if not settings.telegram_enabled:
        return
    client = get_client()
    log.info("Приём обновлений Telegram запущен")
    offset = 0
    delay = 2
    while True:
        try:
            updates = await asyncio.to_thread(client.get_updates, offset)
            delay = 2
            for update in updates:
                offset = max(offset, update["update_id"] + 1)
                try:
                    await asyncio.to_thread(_process_update, client, update)
                except TelegramError as exc:
                    log.warning("Ошибка обработки обновления: %s", exc)
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


def _process_update(client: TelegramClient | DisabledClient, update: dict) -> Optional[str]:
    """Диспетчер обновлений: передаёт в bot.handle_update."""
    from app.bot import handle_update
    today_ = db_today()
    return handle_update(client, update, today_)
