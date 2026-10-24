#!/usr/bin/env bash
# Запуск бэкенда на Linux и macOS
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Создан .env из .env.example — заполните DB_PASSWORD, ADMIN_PASSWORD и данные Telegram."
  exit 1
fi

if [ ! -d .venv ]; then
  "${PYTHON:-python3}" -m venv .venv
fi
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt

echo "== Тесты =="
./.venv/bin/python -m unittest discover -s tests -t . 2>&1 | tail -3

./.venv/bin/python scripts/export_openapi.py

echo "== Сервер: http://localhost:8000/api/v1/docs =="
exec ./.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
