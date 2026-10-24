"""
Групповой Telegram-бот: обработка команд, привязка групп, кнопки.

Бот работает через deep link: администратор генерирует код привязки,
ссылка t.me/bot?startgroup=CODE добавляет бота в группу и привязывает
её к пользователю. Напоминания рассылаются во все привязанные группы.

Обрабатываемые обновления:
- callback_query (кнопка «Оплачено»)
- message (команды /start, /status, /unlink, /help, миграция супергруппы)
- my_chat_member (бот удалён из группы → отвязка)
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Optional

from app import services
from app.database import query_all, query_one
from app.reminders import (
    CALLBACK_PREFIX,
    TelegramClient,
    TelegramError,
    _escape,
    _format_amount,
    build_paid_text,
    close_reminders,
)
from app.security import log_action

log = logging.getLogger("invoice_keeper.bot")

GROUP_TYPES = ("group", "supergroup")

HELP_TEXT = (
    "🤖 <b>Бот напоминаний об оплате</b>\n\n"
    "Этот бот рассылает напоминания о регулярных платежах "
    "в привязанные Telegram-группы.\n\n"
    "<b>Команды в группе:</b>\n"
    "/status — текущие неоплаченные платежи\n"
    "/unlink — отвязать группу (только админ группы)\n"
    "/help — эта справка\n\n"
    "<b>Как привязать группу:</b>\n"
    "1. Администратор нажимает «Привязать Telegram» в панели\n"
    "2. Переходит по ссылке, которая добавляет бота в группу\n"
    "3. Группа привязана — напоминания будут приходить сюда"
)


# =============================================================================
# Главный обработчик
# =============================================================================

def handle_update(
    client: Any,
    update: dict,
    today_: date,
) -> Optional[str]:
    """Обрабатывает одно обновление Telegram. Возвращает короткий результат для логов."""
    # Callback query — кнопка «Оплачено»
    if "callback_query" in update:
        return _handle_callback(client, update["callback_query"], today_)

    # my_chat_member — бот удалён/добавлен
    if "my_chat_member" in update:
        return _handle_my_chat_member(client, update["my_chat_member"])

    # Обычное сообщение
    if "message" in update:
        return _handle_message(client, update["message"], today_)

    return None


# =============================================================================
# Обработка сообщений
# =============================================================================

def _handle_message(client: Any, message: dict, today_: date) -> Optional[str]:
    """Маршрутизация сообщений: миграция, приватный чат, группа."""
    chat = message.get("chat", {})
    chat_type = chat.get("type", "")

    # Миграция группы в супергруппу
    migrate_to = message.get("migrate_to_chat_id")
    if migrate_to is not None:
        old_chat = str(chat.get("id"))
        new_chat = str(migrate_to)
        services.migrate_chat(old_chat, new_chat)
        log.info("Группа мигрировала: %s -> %s", old_chat, new_chat)
        return "migrated"

    # Приватный чат
    if chat_type == "private":
        return _handle_private(client, message)

    # Группа
    if chat_type in GROUP_TYPES:
        return _handle_group(client, message, today_)

    return None


def _handle_private(client: Any, message: dict) -> Optional[str]:
    """Приватный чат: /start с кодом → кнопка «Добавить в группу»."""
    text = message.get("text", "")
    cmd = _parse_command(client, text)
    if cmd is None:
        return None
    command, arg = cmd

    if command != "start" or not arg:
        return None

    # Проверяем код
    info = services.peek_code(arg)
    if info is None:
        client.send("Код привязки недействителен или истёк.", chat_id=str(message["chat"]["id"]))
        return "bad_code"

    # Предлагаем добавить бота в группу
    bot_name = client.bot_username()
    deep_link = f"https://t.me/{bot_name}?startgroup={arg}"
    client.send(
        f"Нажмите кнопку ниже, чтобы добавить бота в группу и привязать её "
        f"к пользователю <b>{_escape(info['username'])}</b>.",
        chat_id=str(message["chat"]["id"]),
        reply_markup={
            "inline_keyboard": [[{"text": "➕ Добавить в группу", "url": deep_link}]]
        },
    )
    return "private_start"


def _handle_group(client: Any, message: dict, today_: date) -> Optional[str]:
    """Обработка команд в группе."""
    text = message.get("text", "")
    cmd = _parse_command(client, text)
    if cmd is None:
        return None
    command, arg = cmd
    chat = message.get("chat", {})
    chat_id = str(chat.get("id"))
    chat_title = chat.get("title", "")
    sender = message.get("from", {})
    sender_id = sender.get("id")

    if command == "start" and arg:
        return _link_group(client, chat_id, chat_title, arg, sender)

    if command == "status":
        return _show_status(client, chat_id, today_)

    if command == "unlink":
        return _unlink_group(client, chat_id, sender_id)

    if command == "help":
        client.send(HELP_TEXT, chat_id=chat_id)
        return "help"

    return None


def _link_group(
    client: Any, chat_id: str, chat_title: str, code: str, sender: dict
) -> str:
    """Привязка группы к пользователю по коду."""
    info = services.peek_code(code)
    if info is None:
        client.send("Код привязки недействителен или истёк.", chat_id=chat_id)
        return "bad_code"

    # Если у пользователя уже была другая группа — уведомляем старую
    if info.get("telegram_chat_id") and info["telegram_chat_id"] != chat_id:
        try:
            client.send(
                "ℹ️ Бот привязан к другой группе. Напоминания больше не будут приходить сюда.",
                chat_id=info["telegram_chat_id"],
            )
        except TelegramError:
            pass

    # Если в этой группе уже был другой пользователь — уведомляем
    existing = services.user_by_chat(chat_id)
    if existing and existing["id"] != info["user_id"]:
        services.unlink_chat(existing["id"])

    tg_username = sender.get("username", "")
    services.link_chat(code, chat_id, tg_username)
    services.update_chat_title(chat_id, chat_title)

    client.send(
        f"✅ Группа привязана к пользователю <b>{_escape(info['username'])}</b>.\n"
        "Напоминания об оплате будут приходить сюда.\n"
        "Для списка команд: /help",
        chat_id=chat_id,
    )
    log.info("Группа %s привязана к пользователю %s", chat_id, info["username"])
    return "linked"


def _show_status(client: Any, chat_id: str, today_: date) -> str:
    """Показывает список неоплаченных платежей."""
    user = services.user_by_chat(chat_id)

    # Проверяем, является ли это общим чатом
    is_common_chat = (chat_id == client.chat_id) if hasattr(client, "chat_id") else False

    if user is None and not is_common_chat:
        client.send(
            "Эта группа не привязана.\n"
            "Привязать Telegram можно через панель управления.",
            chat_id=chat_id,
        )
        return "not_linked"

    text = _status_text(today_)
    client.send(text, chat_id=chat_id)
    return "status"


def _status_text(today_: date) -> str:
    """Формирует текст статуса: общий список неоплаченных платежей."""
    rows = query_all(
        "SELECT v.name, v.amount, v.currency, v.due_date "
        "FROM v_payment_occurrences_detailed v "
        "JOIN recurring_payments rp ON rp.id = v.recurring_payment_id "
        "WHERE v.is_paid = FALSE AND rp.is_deleted = FALSE "
        "ORDER BY v.due_date ASC LIMIT 20"
    )
    if not rows:
        return "✅ Неоплаченных платежей нет."

    lines = ["📋 <b>Неоплаченные платежи:</b>\n"]
    for r in rows:
        due = r["due_date"]
        days_left = (due - today_).days
        if days_left < 0:
            marker = "🔴"
        elif days_left <= 3:
            marker = "🟡"
        else:
            marker = "⚪"
        lines.append(
            f"{marker} <b>{_escape(r['name'])}</b> — "
            f"{_format_amount(r['amount'], r['currency'])} · "
            f"{due.strftime('%d.%m.%Y')}"
        )
    return "\n".join(lines)


def _unlink_group(client: Any, chat_id: str, sender_id: Optional[int]) -> str:
    """Отвязка группы — только для администраторов группы."""
    if sender_id is None or not client.is_chat_admin(chat_id, sender_id):
        client.send("Отвязать группу может только администратор группы.", chat_id=chat_id)
        return "forbidden"

    user = services.user_by_chat(chat_id)
    if user is None:
        client.send("Эта группа не привязана.", chat_id=chat_id)
        return "not_linked"

    services.unlink_by_chat(chat_id)
    client.send("Группа отвязана. Напоминания больше не будут приходить сюда.", chat_id=chat_id)
    log.info("Группа %s отвязана", chat_id)
    return "unlinked"


# =============================================================================
# my_chat_member — бот удалён из группы
# =============================================================================

def _handle_my_chat_member(client: Any, member_update: dict) -> Optional[str]:
    """Обрабатывает изменение статуса бота в чате."""
    chat = member_update.get("chat", {})
    chat_id = str(chat.get("id"))
    new_status = (member_update.get("new_chat_member") or {}).get("status", "")

    if new_status in ("kicked", "left"):
        user = services.user_by_chat(chat_id)
        if user:
            services.unlink_by_chat(chat_id)
            log.info("Бот удалён из группы %s — отвязана", chat_id)
            return "unlinked"
    return None


# =============================================================================
# Callback: кнопка «Оплачено»
# =============================================================================

def _handle_callback(client: Any, callback: dict, today_: date) -> Optional[str]:
    """Обрабатывает нажатие кнопки «Оплачено»."""
    data = callback.get("data") or ""
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id"))

    if not data.startswith(CALLBACK_PREFIX):
        client.answer_callback(callback["id"], "Неизвестная команда")
        return "ignored"

    # Проверяем, что чат привязан или это общий чат
    is_common = (chat_id == client.chat_id) if hasattr(client, "chat_id") else False
    user = services.user_by_chat(chat_id)
    if not is_common and user is None:
        client.answer_callback(callback["id"], "Это действие недоступно в данном чате")
        return "foreign_chat"

    try:
        occurrence_id = int(data[len(CALLBACK_PREFIX):])
    except ValueError:
        client.answer_callback(callback["id"], "Платёж не найден")
        return "bad_data"

    sender = callback.get("from") or {}
    linked = query_one(
        "SELECT username FROM users WHERE telegram_user_id = %s", (sender.get("id"),)
    )
    actor = (
        linked["username"]
        if linked
        else f"telegram:{sender.get('username') or sender.get('id')}"
    )

    occurrence = services.get_occurrence_safe(occurrence_id)
    if occurrence is None:
        client.answer_callback(callback["id"], "Платёж не найден")
        return "not_found"
    if occurrence["is_paid"]:
        client.answer_callback(callback["id"], "Этот период уже отмечен оплаченным")
        return "already_paid"

    updated = services.mark_paid_from_telegram(occurrence_id, actor)
    if updated is None:
        client.answer_callback(callback["id"], "Платёж не найден")
        return "not_found"

    client.answer_callback(callback["id"], "Отмечено как оплачено ✅")
    log_action(
        actor, "user", "MARK_PAID",
        entity_type="payment_occurrence", entity_id=occurrence_id,
        details={
            "source": "telegram_button",
            "actual_payment_date": str(updated["actual_payment_date"]),
        },
    )

    # Обновляем сообщение в текущем чате
    paid_text = build_paid_text(updated, actor)
    if message.get("message_id"):
        try:
            client.finish_message(chat_id, message["message_id"], paid_text)
        except TelegramError as exc:
            log.warning("Не удалось обновить сообщение: %s", exc)

    # Закрываем напоминания во всех остальных чатах
    close_reminders(client, occurrence_id, paid_text, skip=chat_id)

    # Отправляем уведомление «Оплачено» во все чаты, где нет напоминания
    notify_text = (
        f"✅ Платёж оплачен: <b>{_escape(updated['name'])}</b>\n"
        f"Сумма: {_format_amount(updated['amount'], updated['currency'])}\n"
        f"Отметил: {_escape(actor)}"
    )
    # Уведомляем общий чат и все группы, где не было напоминания
    existing_chats = {r["chat_id"] for r in query_all(
        "SELECT DISTINCT chat_id FROM telegram_messages WHERE occurrence_id = %s",
        (occurrence_id,),
    )}
    from app.reminders import targets, DEFAULT_CHAT
    for target in targets(client):
        chat_key = target or DEFAULT_CHAT
        actual = target or (client.chat_id if hasattr(client, "chat_id") else None)
        if chat_key in existing_chats or str(actual) == chat_id:
            continue
        if actual:
            try:
                client.send(notify_text, chat_id=actual)
            except TelegramError:
                pass

    return "paid"


# =============================================================================
# Разбор команд
# =============================================================================

def _parse_command(client: Any, text: str) -> Optional[tuple[str, str]]:
    """Парсит /command@bot arg. Возвращает (command, arg) или None."""
    if not text or not text.startswith("/"):
        return None

    parts = text.split(None, 1)
    cmd_part = parts[0][1:]  # убираем /
    arg = parts[1] if len(parts) > 1 else ""

    # Обрабатываем @botname
    if "@" in cmd_part:
        cmd, bot = cmd_part.split("@", 1)
        bot_name = client.bot_username() if hasattr(client, "bot_username") else ""
        if bot.lower() != bot_name.lower():
            return None  # команда для другого бота
        return (cmd.lower(), arg.strip())

    return (cmd_part.lower(), arg.strip())
