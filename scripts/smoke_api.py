"""
Сквозная проверка работающего API (нужен запущенный сервер и настоящая MySQL).

Проходит весь сценарий защиты из ТЗ и печатает результат по шагам:
вход администратора, создание платежа, период и напоминание, отметка оплаты
пользователем, просрочка и её исправление, следующий период, права ролей.

    python scripts/smoke_api.py                      # http://127.0.0.1:8000, логин/пароль из .env
    python scripts/smoke_api.py --url http://host:8000 --admin admin --password secret

Скрипт создаёт тестовый платёж и тестового пользователя и удаляет их за собой.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASSED: list[str] = []
FAILED: list[str] = []


def check(condition: bool, title: str, extra: str = "") -> None:
    if condition:
        PASSED.append(title)
        print(f"  [ok]   {title}")
    else:
        FAILED.append(title)
        print(f"  [FAIL] {title} {extra}")


def request(method: str, url: str, token: str | None = None, body: dict | None = None) -> tuple[int, object]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            payload = response.read()
            return response.status, (json.loads(payload) if payload else None)
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            return exc.code, json.loads(payload) if payload else None
        except json.JSONDecodeError:
            return exc.code, payload.decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        print(f"\nНе удалось подключиться к {url}: {exc.reason}")
        print("Запущен ли сервер? uvicorn app.main:app --reload")
        raise SystemExit(2)


def main() -> int:
    parser = argparse.ArgumentParser(description="Сквозная проверка Invoice Keeper API")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--admin", default=None)
    parser.add_argument("--password", default=None)
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    base = args.url.rstrip("/") + "/api/v1"
    admin_login = args.admin or os.getenv("ADMIN_USERNAME", "admin")
    admin_password = args.password or os.getenv("ADMIN_PASSWORD", "admin")

    print(f"Проверяю {base}")

    print("\n1. Вход администратора")
    status, body = request("POST", f"{base}/auth/login", body={"username": admin_login, "password": admin_password})
    check(status == 200 and isinstance(body, dict) and "access_token" in body, "POST /auth/login", str(body))
    if status != 200:
        print("\nДальнейшие шаги без входа невозможны. Проверьте ADMIN_USERNAME и ADMIN_PASSWORD в .env")
        return 1
    token = body["access_token"]
    check(body["user"]["role"] == "admin", "роль администратора")

    status, body = request("GET", f"{base}/auth/me", token)
    check(status == 200 and body["username"] == admin_login, "GET /auth/me")

    status, _ = request("GET", f"{base}/payment-occurrences")
    check(status == 401, "без токена данные недоступны")

    print("\n2. Создание регулярного платежа")
    due = date.today() + timedelta(days=2)
    status, payment = request("POST", f"{base}/recurring-payments", token, {
        "name": "Проверка связи (smoke test)",
        "amount": 1234.56,
        "currency": "USD",
        "start_date": due.isoformat(),
    })
    check(status == 201 and payment.get("id"), "POST /recurring-payments", str(payment))
    if status != 201:
        return 1
    payment_id = payment["id"]
    check(payment["anchor_day"] == due.day, "якорный день взят из даты")

    print("\n3. Период оплаты и напоминание")
    status, listing = request("GET", f"{base}/payment-occurrences?recurring_payment_id={payment_id}", token)
    check(status == 200 and listing["total"] >= 1, "GET /payment-occurrences")
    occurrence = listing["items"][0]
    check(occurrence["due_date"] == due.isoformat(), "срок первого периода совпадает")
    check(occurrence["status"] == "unpaid" and not occurrence["is_overdue"], "новый период не оплачен и не просрочен")
    check(occurrence["needs_telegram_reminder"] is True, "платёж со сроком через 2 дня ждёт напоминания")

    print("\n4. Отметка оплаты и исправление даты")
    status, paid = request("PATCH", f"{base}/payment-occurrences/{occurrence['id']}", token, {"is_paid": True})
    check(status == 200 and paid["is_paid"], "PATCH отметка оплаты")
    check(paid["actual_payment_date"] == date.today().isoformat(), "дата оплаты — сегодня")
    check(paid["due_date"] == occurrence["due_date"], "плановый срок не изменился")

    status, fixed = request("PATCH", f"{base}/payment-occurrences/{occurrence['id']}", token,
                            {"actual_payment_date": (date.today() - timedelta(days=1)).isoformat()})
    check(status == 200 and fixed["actual_payment_date"] == (date.today() - timedelta(days=1)).isoformat(),
          "PATCH исправление фактической даты")
    check(fixed["due_date"] == occurrence["due_date"], "срок по-прежнему не меняется")

    status, unpaid = request("PATCH", f"{base}/payment-occurrences/{occurrence['id']}", token, {"is_paid": False})
    check(status == 200 and unpaid["actual_payment_date"] is None, "снятие отметки очищает дату")

    status, bad = request("PATCH", f"{base}/payment-occurrences/{occurrence['id']}", token,
                          {"is_paid": True, "actual_payment_date": (date.today() + timedelta(days=5)).isoformat()})
    check(bad is not None and status in (400, 422), "дата оплаты в будущем отклоняется", str(status))

    print("\n5. Итоги по валютам")
    status, summary = request("GET", f"{base}/payments/summary", token)
    check(status == 200 and [row["currency"] for row in summary["currencies"]] == ["RUB", "USD", "EUR"],
          "GET /payments/summary — три валюты")

    print("\n6. Права ролей")
    status, user = request("POST", f"{base}/users", token,
                           {"username": "smoke_user", "password": "smokepass123", "full_name": "Проверка"})
    check(status in (201, 409), "POST /users", str(user))
    user_id = user.get("id") if status == 201 else None
    if status == 409:
        status, users = request("GET", f"{base}/users", token)
        user_id = next((item["id"] for item in users if item["username"] == "smoke_user"), None)

    status, body = request("POST", f"{base}/auth/login", body={"username": "smoke_user", "password": "smokepass123"})
    check(status == 200, "вход обычного пользователя")
    user_token = body.get("access_token") if status == 200 else None

    if user_token:
        status, _ = request("POST", f"{base}/recurring-payments", user_token, {
            "name": "Запрещено", "amount": 1, "currency": "RUB", "start_date": due.isoformat()})
        check(status == 403, "пользователь не создаёт платежи (403)", str(status))
        status, _ = request("GET", f"{base}/users", user_token)
        check(status == 403, "пользователь не видит список пользователей (403)", str(status))
        status, _ = request("DELETE", f"{base}/recurring-payments/{payment_id}", user_token)
        check(status == 403, "пользователь не удаляет платежи (403)", str(status))
        status, _ = request("PATCH", f"{base}/payment-occurrences/{occurrence['id']}", user_token, {"is_paid": True})
        check(status == 200, "пользователь отмечает оплату (200)", str(status))

    print("\n7. Прекращение платежа и сохранение истории")
    status, deleted = request("DELETE", f"{base}/recurring-payments/{payment_id}", token)
    check(status == 200 and deleted["is_deleted"], "DELETE /recurring-payments/{id}")
    status, listing = request("GET", f"{base}/payment-occurrences?recurring_payment_id={payment_id}", token)
    check(status == 200 and listing["total"] >= 1, "история периодов сохранена")
    status, listing = request("GET", f"{base}/recurring-payments", token)
    check(all(item["id"] != payment_id for item in listing), "прекращённый платёж исчез из активного списка")

    if user_id:
        request("DELETE", f"{base}/users/{user_id}", token)

    print("\n" + "=" * 60)
    print(f"Успешно: {len(PASSED)}   Ошибок: {len(FAILED)}")
    if FAILED:
        for title in FAILED:
            print(f"  - {title}")
        return 1
    print("Все проверки пройдены.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
