@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title Invoice Keeper API

echo.
echo ===== Invoice Keeper: запуск бэкенда =====
echo.

rem --- 1. Python ---
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo [ОШИБКА] Python 3.11+ не найден. Установите его с https://www.python.org/downloads/
  echo          и отметьте "Add python.exe to PATH".
  pause & exit /b 1
)

rem --- 2. Файл .env ---
if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo [ВНИМАНИЕ] Создан файл .env из .env.example.
  echo            Заполните DB_PASSWORD, ADMIN_PASSWORD и данные Telegram, затем запустите снова.
  notepad .env
  pause & exit /b 1
)

rem --- 3. Виртуальное окружение и зависимости ---
if not exist ".venv\Scripts\python.exe" (
  echo Создаю виртуальное окружение...
  %PY% -m venv .venv || (echo [ОШИБКА] Не удалось создать .venv & pause & exit /b 1)
)
echo Проверяю зависимости...
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || (
  echo [ОШИБКА] Не удалось установить зависимости. Проверьте интернет. & pause & exit /b 1
)

rem --- 4. Тесты бизнес-логики (без базы) ---
echo.
echo ===== Тесты =====
".venv\Scripts\python.exe" -m unittest discover -s tests -t . 2>&1 | findstr /R /C:"^Ran " /C:"^OK" /C:"^FAILED"

rem --- 5. Спецификация OpenAPI ---
".venv\Scripts\python.exe" scripts\export_openapi.py

rem --- 6. Запуск ---
echo.
echo ===== Сервер: http://localhost:8000/api/v1/docs   (Ctrl+C — остановить) =====
start "" cmd /c "timeout /t 3 >nul & start http://localhost:8000/api/v1/docs"
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8000

pause
