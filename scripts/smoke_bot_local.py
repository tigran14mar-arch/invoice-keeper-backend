"""Проверка группового бота на локальной MySQL без запросов в Telegram.

Запуск: .venv/bin/python scripts/smoke_bot_local.py
Использует только изолированную базу run-local.sh. Создаёт и убирает свои записи.
"""
from pathlib import Path
import os
import sys
import uuid
from datetime import timedelta
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.update(DB_UNIX_SOCKET=f"/tmp/invoice-keeper-{os.getuid()}/mysql.sock",
                  DB_HOST="localhost", DB_USER="root", DB_PASSWORD="",
                  DB_NAME="invoice_keeper", TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="")

from app import bot, services, reminders
from app.database import check_schema, execute, today
from app.schemas import UserCreate, RecurringPaymentCreate


def main():
    check_schema()
    suffix = uuid.uuid4().hex[:12]
    user = services.create_user(UserCreate(username=f"bot_test_{suffix}", password=uuid.uuid4().hex))
    payment = None
    checks = 0
    def check(condition, title):
        nonlocal checks
        assert condition, title
        checks += 1
        print(f"[ok] {title}")

    calls = []
    def transport(method, payload, **kwargs):
        calls.append((method, payload))
        if method == "getMe":
            return {"username": "LocalTestBot"}
        if method == "getChatMember":
            return {"status": "administrator"}
        if method == "sendMessage":
            return {"message_id": len(calls)}
        return True

    client = reminders.TelegramClient("local-test-placeholder")
    chat_id = "-" + str(int(suffix, 16))
    def message(text, kind="group", chat=chat_id):
        return {"message": {"text": text, "chat": {"id": chat, "type": kind, "title": "Local test"},
                            "from": {"id": 42, "username": "local_test"}}}
    try:
        with patch.object(client, "_call", side_effect=transport), patch("urllib.request.urlopen", side_effect=AssertionError("Network forbidden")):
            current = today()
            check(bot.handle_update(client, message("/start", "private", "42"), current) == "help", "Личный /start отвечает")
            check(bot.handle_update(client, message("/help", "private", "42"), current) == "help", "Личный /help отвечает")
            check(bot.handle_update(client, message("/status@OtherBot"), current) is None, "Чужие команды игнорируются")
            check(bot.handle_update(client, message("/status"), current) == "not_linked", "Непривязанная группа не видит платежи")
            code = services.create_link_code(user["id"])["code"]
            check(bot.handle_update(client, message(f"/start@LocalTestBot {code}"), current) == "linked", "Группа привязывается")
            check(services.peek_code(code) is None, "Код привязки одноразовый")
            check(bot.handle_update(client, message("/status"), current) == "status", "Статус привязанной группы")
            payment = services.create_recurring_payment(RecurringPaymentCreate(name=f"Bot test {suffix}", amount=100,
                         currency="RUB", start_date=current + timedelta(days=2)), "local-test")
            occurrences, _ = services.list_occurrences(recurring_payment_id=payment["id"])
            occurrence = occurrences[0]
            delivered = reminders.broadcast(client, "Test reminder", occurrence["id"])
            reminders.remember_messages(occurrence["id"], delivered)
            check(any(chat == chat_id for chat, _ in delivered), "Напоминание адресовано привязанной группе")
            callback = {"callback_query": {"id": "test", "data": f"paid:{occurrence['id']}", "from": {"id": 42},
                        "message": {"chat": {"id": chat_id}, "message_id": delivered[0][1]}}}
            check(bot.handle_update(client, callback, current) == "paid", "Кнопка Оплачено")
            check(services.get_occurrence_safe(occurrence["id"])["is_paid"], "Оплата сохранена в MySQL")
            check(bot.handle_update(client, callback, current) == "already_paid", "Повторное нажатие без повторной оплаты")
            callback["callback_query"]["message"]["chat"]["id"] = "-999999"
            check(bot.handle_update(client, callback, current) == "foreign_chat", "Кнопка из чужой группы отклоняется")
            with patch.object(client, "is_chat_admin", return_value=False):
                check(bot.handle_update(client, message("/unlink"), current) == "forbidden", "Отвязка доступна только администратору")
            check(bot.handle_update(client, message("/unlink"), current) == "unlinked", "Администратор отвязывает группу")
            check(services.user_by_chat(chat_id) is None, "Привязка удалена из MySQL")
        print(f"Проверок бота: {checks}; обращений к Telegram: 0")
    finally:
        if payment:
            execute("DELETE FROM payment_occurrences WHERE recurring_payment_id = %s", (payment["id"],))
            execute("DELETE FROM recurring_payments WHERE id = %s", (payment["id"],))
        execute("DELETE FROM users WHERE id = %s", (user["id"],))


if __name__ == "__main__":
    main()
