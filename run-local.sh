#!/usr/bin/env bash
# Локальная копия: отдельная MySQL, API только на loopback, Telegram выключен.
set -euo pipefail
cd "$(dirname "$0")"
runtime="$PWD/.local-runtime"
mysql_dir="$PWD/.local-tools/mysql"
socket_dir="/tmp/invoice-keeper-$(id -u)"
mkdir -p "$runtime" "$socket_dir"
chmod 700 "$runtime" "$socket_dir"
if [[ ! -x "$mysql_dir/bin/mysqld" || ! -x .venv/bin/python ]]; then
  echo "Нужны .venv (Python 3.12 + requirements.txt) и MySQL в .local-tools/mysql. См. README."
  exit 1
fi
if [[ ! -d "$runtime/mysql-data" ]]; then
  echo "Локальная база ещё не подготовлена. См. README; существующую базу не перезаписываем."
  exit 1
fi
if ! "$mysql_dir/bin/mysqladmin" --no-defaults --socket="$socket_dir/mysql.sock" -u root ping >/dev/null 2>&1; then
  "$mysql_dir/bin/mysqld" --no-defaults --basedir="$mysql_dir" \
    --datadir="$runtime/mysql-data" --socket="$socket_dir/mysql.sock" \
    --skip-networking --mysqlx=0 --pid-file="$runtime/mysql.pid" \
    --log-error="$runtime/mysql.log" --daemonize
fi
export DB_HOST=localhost DB_PORT=3306 DB_USER=root DB_PASSWORD='' DB_NAME=invoice_keeper
export DB_UNIX_SOCKET="$socket_dir/mysql.sock"
export SESSION_COOKIE_SECURE=false
if [[ "${LOCAL_TELEGRAM_ENABLED:-false}" != true ]]; then
  export TELEGRAM_BOT_TOKEN='' TELEGRAM_CHAT_ID=''
fi
echo "API: http://127.0.0.1:8000/api/v1/docs"
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
