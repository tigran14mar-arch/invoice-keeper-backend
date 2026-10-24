"""
API приложения Invoice Keeper (FastAPI).

Этот файл — только транспортный слой: принять запрос, проверить права,
вызвать нужный метод бэкенда, вернуть ответ. Бизнес-логики здесь нет,
она лежит в app/services.py, app/security.py и app/reminders.py.

Все адреса начинаются с /api/v1, как требует техническое задание.
Документация Swagger: GET /api/v1/docs, спецификация: GET /api/v1/openapi.json.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import JSONResponse, RedirectResponse

from app import reminders, services
from app.config import settings
from app.database import DatabaseError, check_schema, close_pool, init_pool, query_value
from app.schemas import (
    AuditLogOut,
    BudgetOut,
    BudgetUpsert,
    CalendarOut,
    DashboardOut,
    ErrorResponse,
    LoginRequest,
    LoginResponse,
    Message,
    OccurrenceListOut,
    PaymentOccurrenceOut,
    PaymentOccurrenceUpdate,
    RecurringPaymentCreate,
    RecurringPaymentDeleteOut,
    RecurringPaymentEditOut,
    RecurringPaymentOut,
    RecurringPaymentUpdate,
    ReminderRunOut,
    SettingsOut,
    SettingsUpdate,
    SummaryOut,
    UserCreate,
    UserOut,
    UserUpdate,
)
from app.security import (
    CurrentUser,
    authenticate,
    cleanup_expired_sessions,
    client_ip,
    create_session,
    current_user,
    log_action,
    require_admin,
    revoke_session,
    sync_env_admin,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("invoice_keeper")

API = "/api/v1"
ADMIN_ONLY = "Доступно только администратору."

BACKGROUND_TASKS: list[asyncio.Task] = []


def _export_openapi() -> None:
    """Сохраняет актуальную спецификацию рядом с кодом: openapi.json и openapi.yaml.

    Требование кейса — приложить спецификацию файлом. Файл обновляется при каждом
    запуске, поэтому не может разойтись с реальным API.
    """
    try:
        import json
        from pathlib import Path

        import yaml

        spec = app.openapi()
        root = Path(__file__).resolve().parent.parent
        (root / "openapi.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
        (root / "openapi.yaml").write_text(
            yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        log.info("Спецификация OpenAPI сохранена: openapi.json, openapi.yaml")
    except Exception as exc:  # noqa: BLE001 — выгрузка не должна мешать запуску
        log.warning("Не удалось сохранить спецификацию OpenAPI: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Старт: подключение к базе, синхронизация администратора из .env, фоновые задачи."""
    init_pool()
    check_schema()
    sync_env_admin()
    cleanup_expired_sessions()
    created = services.ensure_all_periods()
    log.info("Приложение готово. Периодов создано при старте: %s", created)
    if settings.admin_password in ("", "admin", "change-me"):
        log.warning("ADMIN_PASSWORD в .env не задан или слишком простой — смените его перед показом жюри")
    _export_openapi()
    if not settings.telegram_enabled:
        log.warning("Telegram не настроен: напоминания отправляться не будут "
                    "(заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env)")
    if settings.scheduler_enabled:
        BACKGROUND_TASKS.append(asyncio.create_task(reminders.scheduler_task()))
        BACKGROUND_TASKS.append(asyncio.create_task(reminders.callback_task()))
    try:
        yield
    finally:
        for task in BACKGROUND_TASKS:
            task.cancel()
        for task in BACKGROUND_TASKS:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        BACKGROUND_TASKS.clear()
        close_pool()
        log.info("Приложение остановлено")


app = FastAPI(
    title="Invoice Keeper API",
    version="1.0.0",
    description=(
        "Напоминания о регулярных платежах: аренда, подписки, серверы.\n\n"
        "* Две роли: администратор (задаётся в `.env`) и пользователь.\n"
        "* Три валюты: RUB, USD, EUR — суммы считаются отдельно, без конвертации.\n"
        "* Напоминание в Telegram минимум за 3 дня до срока, без дублей.\n"
        "* Авторизация по токену сессии: заголовок `Authorization: Bearer <token>` "
        "или HttpOnly-cookie, которую ставит `POST /api/v1/auth/login`."
    ),
    openapi_url=f"{API}/openapi.json",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
    responses={401: {"model": ErrorResponse, "description": "Требуется авторизация"}},
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- обработка ошибок

@app.exception_handler(services.ServiceError)
async def service_error_handler(_: Request, exc: services.ServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


@app.exception_handler(DatabaseError)
async def database_error_handler(_: Request, exc: DatabaseError) -> JSONResponse:
    log.error("Ошибка базы данных: %s", exc)
    return JSONResponse(status_code=503, content={"detail": str(exc)})


# --------------------------------------------------------------------------- документация

@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url=f"{API}/docs")


@app.get(f"{API}/docs", include_in_schema=False)
async def swagger_ui() -> Response:
    return get_swagger_ui_html(openapi_url=f"{API}/openapi.json", title="Invoice Keeper API — документация")


@app.get(f"{API}/redoc", include_in_schema=False)
async def redoc_ui() -> Response:
    return get_redoc_html(openapi_url=f"{API}/openapi.json", title="Invoice Keeper API")


@app.get(f"{API}/health", tags=["Служебные"], summary="Проверка работоспособности")
async def health() -> dict:
    """Проверяет, что приложение живо и база отвечает."""
    try:
        today = query_value("SELECT fn_today()")
        return {"status": "ok", "database": "ok", "today": today}
    except DatabaseError as exc:
        return {"status": "degraded", "database": str(exc)}


# =============================================================================
# Авторизация
# =============================================================================

def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.cookie_name,
        value=token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


@app.post(
    f"{API}/auth/login",
    response_model=LoginResponse,
    tags=["Авторизация"],
    summary="Вход по логину и паролю",
    responses={401: {"model": ErrorResponse, "description": "Неверный логин или пароль"}},
)
async def login(payload: LoginRequest, request: Request, response: Response) -> LoginResponse:
    """Проверяет логин и пароль, создаёт сессию.

    Токен возвращается в теле ответа и ставится HttpOnly-cookie, поэтому
    веб-панель может работать без ручной передачи заголовка.
    """
    user = authenticate(payload.username, payload.password)
    if user is None:
        log_action(
            payload.username, "user", "LOGIN_FAILED",
            details={"reason": "Неверный логин или пароль"}, ip_address=client_ip(request),
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверный логин или пароль")

    public = {key: value for key, value in user.items() if key != "password_hash"}
    token, expires_at = create_session(user)
    _set_session_cookie(response, token)
    log_action(user["username"], user["role"], "LOGIN", entity_type="user", entity_id=user["id"],
               ip_address=client_ip(request))
    return LoginResponse(access_token=token, expires_at=expires_at, user=UserOut(**public))


@app.get(
    f"{API}/auth/me",
    response_model=UserOut,
    tags=["Авторизация"],
    summary="Текущий пользователь",
)
async def me(user: Annotated[CurrentUser, Depends(current_user)]) -> UserOut:
    """Кто вошёл в систему: логин, роль и профиль. Веб-панель вызывает это при открытии страницы."""
    if user.id is not None:
        return UserOut(**services.get_user(user.id))
    return UserOut(id=None, username=user.username, full_name=user.full_name, role=user.role, is_env_admin=True)


@app.post(
    f"{API}/auth/logout",
    response_model=Message,
    tags=["Авторизация"],
    summary="Завершение текущей сессии",
)
async def logout(
    request: Request,
    response: Response,
    user: Annotated[CurrentUser, Depends(current_user)],
) -> Message:
    """Отзывает сессию: токен сразу перестаёт работать, cookie удаляется."""
    revoke_session(user.session_token_hash)
    response.delete_cookie(settings.cookie_name, path="/")
    log_action(user.username, user.role, "LOGOUT", ip_address=client_ip(request))
    return Message(detail="Сессия завершена")


# =============================================================================
# Регулярные платежи
# =============================================================================

@app.get(
    f"{API}/recurring-payments",
    response_model=list[RecurringPaymentOut],
    tags=["Регулярные платежи"],
    summary="Список регулярных платежей",
)
async def get_recurring_payments(
    _: Annotated[CurrentUser, Depends(current_user)],
    include_deleted: bool = Query(False, description="Показывать прекращённые платежи"),
    currency: Optional[Literal["RUB", "USD", "EUR"]] = Query(None),
    search: Optional[str] = Query(None, max_length=255, description="Поиск по названию"),
) -> list[dict]:
    """Шаблоны платежей: название, сумма, валюта, периодичность и срок ближайшей оплаты."""
    return services.list_recurring_payments(include_deleted=include_deleted, currency=currency, search=search)


@app.get(
    f"{API}/recurring-payments/{{payment_id}}",
    response_model=RecurringPaymentOut,
    tags=["Регулярные платежи"],
    summary="Один регулярный платёж",
    responses={404: {"model": ErrorResponse, "description": "Платёж не найден"}},
)
async def get_recurring_payment(
    payment_id: int,
    _: Annotated[CurrentUser, Depends(current_user)],
) -> dict:
    return services.get_recurring_payment(payment_id)


@app.post(
    f"{API}/recurring-payments",
    response_model=RecurringPaymentOut,
    status_code=status.HTTP_201_CREATED,
    tags=["Регулярные платежи"],
    summary="Создание регулярного платежа",
    description=ADMIN_ONLY + " Создаёт шаблон и сразу первый период оплаты.",
    responses={403: {"model": ErrorResponse, "description": "Только администратор"}},
)
async def create_recurring_payment(
    payload: RecurringPaymentCreate,
    request: Request,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> dict:
    """Добавляет регулярный платёж.

    Если до срока осталось меньше, чем `remind_days_before` дней, напоминание уйдёт
    в ближайшую проверку планировщика — то есть практически сразу.
    """
    payment = services.create_recurring_payment(payload, admin.username)
    log_action(
        admin.username, admin.role, "CREATE_RECURRING",
        entity_type="recurring_payment", entity_id=payment["id"],
        details={"name": payment["name"], "amount": str(payment["amount"]), "currency": payment["currency"],
                 "start_date": str(payment["start_date"])},
        ip_address=client_ip(request),
    )
    return payment


@app.patch(
    f"{API}/recurring-payments/{{payment_id}}",
    response_model=RecurringPaymentEditOut,
    tags=["Регулярные платежи"],
    summary="Изменение регулярного платежа",
    description=ADMIN_ONLY + " Меняются шаблон и будущие неоплаченные периоды; история не переписывается.",
    responses={
        403: {"model": ErrorResponse, "description": "Только администратор"},
        404: {"model": ErrorResponse, "description": "Платёж не найден"},
        409: {"model": ErrorResponse, "description": "Платёж прекращён или новая дата конфликтует с историей"},
    },
)
async def update_recurring_payment(
    payment_id: int,
    payload: RecurringPaymentUpdate,
    request: Request,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> dict:
    """Передавайте только поля, которые меняются.

    * `name`, `remind_days_before` — меняются сразу везде.
    * `amount`, `currency` — меняются у шаблона и у неоплаченных периодов со сроком сегодня или позже.
    * `start_date`, `anchor_day` — смена графика: будущие неоплаченные периоды создаются заново.

    Оплаченные и прошедшие периоды остаются без изменений.
    """
    payment, changes = services.update_recurring_payment(payment_id, payload)
    if changes:
        log_action(
            admin.username, admin.role, "UPDATE_RECURRING",
            entity_type="recurring_payment", entity_id=payment_id,
            details={
                "name": payment["name"],
                "changes": {key: {"old": str(old), "new": str(new)} for key, (old, new) in changes.items()},
                "updated_periods": payment["updated_periods"],
                "removed_periods": payment["removed_periods"],
                "created_periods": payment["created_periods"],
            },
            ip_address=client_ip(request),
        )
    return payment


@app.delete(
    f"{API}/recurring-payments/{{payment_id}}",
    response_model=RecurringPaymentDeleteOut,
    tags=["Регулярные платежи"],
    summary="Прекращение или полное удаление регулярного платежа",
    description=ADMIN_ONLY + " По умолчанию — прекращение: напоминания и новые периоды прекращаются, "
                "история оплат сохраняется. С `?permanent=true` платёж удаляется полностью вместе с историей.",
    responses={
        403: {"model": ErrorResponse, "description": "Только администратор"},
        404: {"model": ErrorResponse, "description": "Платёж не найден"},
        409: {"model": ErrorResponse, "description": "Платёж уже прекращён"},
    },
)
async def delete_recurring_payment(
    payment_id: int,
    request: Request,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    permanent: bool = Query(
        False, description="true — удалить платёж и все его периоды без возможности восстановления",
    ),
) -> dict:
    """Мягкое удаление (по умолчанию): платёж помечается прекращённым, прошлые периоды остаются в истории."""
    if permanent:
        payment, removed = services.delete_recurring_payment_permanently(payment_id)
        log_action(
            admin.username, admin.role, "DELETE_RECURRING_PERMANENT",
            entity_type="recurring_payment", entity_id=payment_id,
            details={"name": payment["name"], "amount": str(payment["amount"]),
                     "currency": payment["currency"], "removed_periods": removed},
            ip_address=client_ip(request),
        )
        return {**payment, "permanent": True, "removed_periods": removed}

    payment = services.delete_recurring_payment(payment_id)
    log_action(
        admin.username, admin.role, "DELETE_RECURRING",
        entity_type="recurring_payment", entity_id=payment_id,
        details={"name": payment["name"]}, ip_address=client_ip(request),
    )
    return {**payment, "permanent": False, "removed_periods": 0}


# =============================================================================
# Периоды оплаты
# =============================================================================

@app.get(
    f"{API}/payment-occurrences",
    response_model=OccurrenceListOut,
    tags=["Периоды оплаты"],
    summary="Список платежей: оплаченные, неоплаченные и просроченные",
)
async def get_payment_occurrences(
    _: Annotated[CurrentUser, Depends(current_user)],
    status_filter: Optional[Literal["paid", "unpaid"]] = Query(
        None, alias="status", description="paid — оплаченные, unpaid — неоплаченные",
    ),
    overdue: Optional[bool] = Query(None, description="true — только просроченные"),
    currency: Optional[Literal["RUB", "USD", "EUR"]] = Query(None),
    recurring_payment_id: Optional[int] = Query(None, ge=1),
    date_from: Optional[date] = Query(None, description="Срок оплаты не раньше этой даты"),
    date_to: Optional[date] = Query(None, description="Срок оплаты не позже этой даты"),
    include_deleted_recurring: bool = Query(True, description="Включать историю прекращённых платежей"),
    search: Optional[str] = Query(None, max_length=255),
    sort: Literal["due_date", "-due_date", "amount", "-amount", "name"] = Query("due_date"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    """Каждый период — своя дата, свой статус и своя просрочка.

    Просрочка считается динамически: неоплаченный период со сроком раньше сегодняшнего дня
    либо оплаченный позже срока. Оплата в день срока считается своевременной.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from не может быть позже date_to")
    items, total = services.list_occurrences(
        status=status_filter, overdue=overdue, currency=currency,
        recurring_payment_id=recurring_payment_id, date_from=date_from, date_to=date_to,
        include_deleted_recurring=include_deleted_recurring, search=search,
        sort=sort, limit=limit, offset=offset,
    )
    return {"total": total, "limit": limit, "offset": offset, "items": items}


@app.get(
    f"{API}/payment-occurrences/{{occurrence_id}}",
    response_model=PaymentOccurrenceOut,
    tags=["Периоды оплаты"],
    summary="Получение конкретного платежа",
    responses={404: {"model": ErrorResponse, "description": "Период не найден"}},
)
async def get_payment_occurrence(
    occurrence_id: int,
    _: Annotated[CurrentUser, Depends(current_user)],
) -> dict:
    return services.get_occurrence(occurrence_id)


@app.patch(
    f"{API}/payment-occurrences/{{occurrence_id}}",
    response_model=PaymentOccurrenceOut,
    tags=["Периоды оплаты"],
    summary="Подтверждение или отмена оплаты, исправление фактической даты",
    responses={404: {"model": ErrorResponse, "description": "Период не найден"}},
)
async def patch_payment_occurrence(
    occurrence_id: int,
    payload: PaymentOccurrenceUpdate,
    request: Request,
    user: Annotated[CurrentUser, Depends(current_user)],
) -> dict:
    """Доступно и администратору, и пользователю.

    * `is_paid = true` — оплата подтверждена; без даты подставляется сегодняшняя.
    * `is_paid = false` — отметка снимается, фактическая дата очищается.
    * `actual_payment_date` — исправление даты у уже оплаченного периода:
      просрочка пересчитывается, а плановый срок остаётся прежним.
    """
    occurrence, action = services.update_occurrence(occurrence_id, payload, user.username)
    log_action(
        user.username, user.role, action,
        entity_type="payment_occurrence", entity_id=occurrence_id,
        details={
            "name": occurrence["name"],
            "is_paid": bool(occurrence["is_paid"]),
            "actual_payment_date": str(occurrence["actual_payment_date"]) if occurrence["actual_payment_date"] else None,
            "due_date": str(occurrence["due_date"]),
        },
        ip_address=client_ip(request),
    )
    return occurrence


# =============================================================================
# Итоги, календарь, главная страница
# =============================================================================

@app.get(
    f"{API}/payments/summary",
    response_model=SummaryOut,
    tags=["Итоги"],
    summary="Итоговые суммы платежей по валютам",
)
async def payments_summary(
    _: Annotated[CurrentUser, Depends(current_user)],
    date_from: Optional[date] = Query(None, description="Начало периода по сроку оплаты"),
    date_to: Optional[date] = Query(None, description="Конец периода по сроку оплаты"),
    include_deleted_recurring: bool = Query(True),
) -> dict:
    """Суммы считаются отдельно по каждой валюте, без конвертации и курсов.

    В ответе всегда три валюты — RUB, USD и EUR, даже если по какой-то из них платежей нет.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from не может быть позже date_to")
    rows = services.summary_by_currency(
        date_from=date_from, date_to=date_to, include_deleted_recurring=include_deleted_recurring
    )
    return {"today": services.db_today(), "date_from": date_from, "date_to": date_to, "currencies": rows}


@app.get(
    f"{API}/payments/calendar",
    response_model=CalendarOut,
    tags=["Итоги"],
    summary="Календарь оплат на месяц",
)
async def payments_calendar(
    _: Annotated[CurrentUser, Depends(current_user)],
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
) -> dict:
    """Платежи по дням месяца — для календаря в веб-панели."""
    return services.calendar_month(year, month)


@app.get(
    f"{API}/dashboard",
    response_model=DashboardOut,
    tags=["Итоги"],
    summary="Данные главной страницы одним запросом",
)
async def dashboard(user: Annotated[CurrentUser, Depends(current_user)]) -> dict:
    """Профиль, итоги за месяц и за всё время, просрочки, ближайшие оплаты и последние оплаченные.

    Отдельный адрес нужен, чтобы при открытии главной страницы не делать пять запросов подряд.
    """
    profile = services.get_user(user.id) if user.id is not None else {
        "id": None, "username": user.username, "full_name": user.full_name,
        "role": user.role, "is_env_admin": True, "is_active": True, "telegram_user_id": None,
        "created_at": None, "updated_at": None,
    }
    return services.dashboard(profile)


# =============================================================================
# Пользователи (только администратор)
# =============================================================================

@app.get(
    f"{API}/users",
    response_model=list[UserOut],
    tags=["Пользователи"],
    summary="Список пользователей",
    description=ADMIN_ONLY,
    responses={403: {"model": ErrorResponse, "description": "Только администратор"}},
)
async def get_users(
    _: Annotated[CurrentUser, Depends(require_admin)],
    include_inactive: bool = Query(True),
    search: Optional[str] = Query(None, max_length=100),
) -> list[dict]:
    return services.list_users(include_inactive=include_inactive, search=search)


@app.post(
    f"{API}/users",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    tags=["Пользователи"],
    summary="Создание обычного пользователя",
    description=ADMIN_ONLY + " Роль admin через API выдать нельзя — администратор задаётся в .env.",
    responses={
        403: {"model": ErrorResponse, "description": "Только администратор"},
        409: {"model": ErrorResponse, "description": "Логин уже занят"},
    },
)
async def create_user(
    payload: UserCreate,
    request: Request,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> dict:
    user = services.create_user(payload)
    log_action(
        admin.username, admin.role, "CREATE_USER", entity_type="user", entity_id=user["id"],
        details={"username": user["username"]}, ip_address=client_ip(request),
    )
    return user


@app.patch(
    f"{API}/users/{{user_id}}",
    response_model=UserOut,
    tags=["Пользователи"],
    summary="Изменение пользователя",
    description=ADMIN_ONLY + " Учётную запись администратора из .env изменить нельзя.",
    responses={
        403: {"model": ErrorResponse, "description": "Только администратор / защищённая запись"},
        404: {"model": ErrorResponse, "description": "Пользователь не найден"},
        409: {"model": ErrorResponse, "description": "Логин уже занят"},
    },
)
async def update_user(
    user_id: int,
    payload: UserUpdate,
    request: Request,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> dict:
    """Смена логина, имени, пароля, Telegram ID и признака активности.

    Смена пароля и отключение учётной записи завершают её активные сессии.
    """
    user = services.update_user(user_id, payload)
    log_action(
        admin.username, admin.role, "UPDATE_USER", entity_type="user", entity_id=user_id,
        details={"fields": sorted(payload.model_dump(exclude_unset=True).keys())},
        ip_address=client_ip(request),
    )
    return user


@app.delete(
    f"{API}/users/{{user_id}}",
    response_model=Message,
    tags=["Пользователи"],
    summary="Удаление пользователя",
    description=ADMIN_ONLY + " Учётную запись администратора из .env удалить нельзя.",
    responses={
        403: {"model": ErrorResponse, "description": "Только администратор / защищённая запись"},
        404: {"model": ErrorResponse, "description": "Пользователь не найден"},
    },
)
async def delete_user(
    user_id: int,
    request: Request,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> Message:
    user = services.delete_user(user_id)
    log_action(
        admin.username, admin.role, "DELETE_USER", entity_type="user", entity_id=user_id,
        details={"username": user["username"]}, ip_address=client_ip(request),
    )
    return Message(detail=f"Пользователь {user['username']} удалён")


# =============================================================================
# Настройки, напоминания, бюджеты, журнал
# =============================================================================

@app.get(
    f"{API}/settings",
    response_model=SettingsOut,
    tags=["Служебные"],
    summary="Часовой пояс, сегодняшняя дата и демо-сдвиг",
)
async def get_settings_state(_: Annotated[CurrentUser, Depends(current_user)]) -> dict:
    return services.get_settings_state()


@app.patch(
    f"{API}/settings",
    response_model=SettingsOut,
    tags=["Служебные"],
    summary="Сдвиг «сегодня» для демонстрации",
    description=ADMIN_ONLY + " Позволяет показать просрочку и напоминания, не дожидаясь реальных дат.",
    responses={403: {"model": ErrorResponse, "description": "Только администратор"}},
)
async def update_settings(
    payload: SettingsUpdate,
    request: Request,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> dict:
    """`override_today` сдвигает дату для всей системы: просрочка, напоминания и итоги
    пересчитываются сразу. `null` возвращает реальную дату."""
    state = services.set_override_today(payload.override_today)
    log_action(
        admin.username, admin.role, "SET_DEMO_DATE",
        details={"override_today": str(payload.override_today) if payload.override_today else None},
        ip_address=client_ip(request),
    )
    return state


@app.post(
    f"{API}/reminders/run",
    response_model=ReminderRunOut,
    tags=["Служебные"],
    summary="Ручной запуск проверки напоминаний",
    description=ADMIN_ONLY + " Достраивает периоды и отправляет готовые напоминания в Telegram.",
    responses={403: {"model": ErrorResponse, "description": "Только администратор"}},
)
async def run_reminders(
    request: Request,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> dict:
    """Полезно на демонстрации: не ждать очередного цикла планировщика."""
    result = await asyncio.to_thread(reminders.run_reminder_cycle)
    log_action(admin.username, admin.role, "RUN_REMINDERS", details=result, ip_address=client_ip(request))
    return result


@app.get(
    f"{API}/budgets",
    response_model=list[BudgetOut],
    tags=["Бюджеты"],
    summary="Плановые бюджеты по валютам",
)
async def get_budgets(
    _: Annotated[CurrentUser, Depends(current_user)],
    period_type: Optional[Literal["monthly", "yearly"]] = Query(None),
    year: Optional[int] = Query(None, ge=2000, le=2100),
    month: Optional[int] = Query(None, ge=0, le=12),
) -> list[dict]:
    """К каждому бюджету добавлены фактические суммы: сколько уже оплачено и сколько запланировано."""
    return services.list_budgets(period_type=period_type, year=year, month=month)


@app.put(
    f"{API}/budgets",
    response_model=BudgetOut,
    tags=["Бюджеты"],
    summary="Создание или изменение бюджета",
    description=ADMIN_ONLY,
    responses={403: {"model": ErrorResponse, "description": "Только администратор"}},
)
async def upsert_budget(
    payload: BudgetUpsert,
    request: Request,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> dict:
    budget = services.upsert_budget(payload)
    log_action(
        admin.username, admin.role, "UPSERT_BUDGET", entity_type="budget", entity_id=budget["id"],
        details={"currency": budget["currency"], "planned_limit": str(budget["planned_limit"])},
        ip_address=client_ip(request),
    )
    return budget


@app.delete(
    f"{API}/budgets/{{budget_id}}",
    response_model=Message,
    tags=["Бюджеты"],
    summary="Удаление бюджета",
    description=ADMIN_ONLY,
    responses={
        403: {"model": ErrorResponse, "description": "Только администратор"},
        404: {"model": ErrorResponse, "description": "Бюджет не найден"},
    },
)
async def delete_budget(
    budget_id: int,
    request: Request,
    admin: Annotated[CurrentUser, Depends(require_admin)],
) -> Message:
    services.delete_budget(budget_id)
    log_action(admin.username, admin.role, "DELETE_BUDGET", entity_type="budget", entity_id=budget_id,
               ip_address=client_ip(request))
    return Message(detail="Бюджет удалён")


@app.get(
    f"{API}/audit-logs",
    response_model=list[AuditLogOut],
    tags=["Служебные"],
    summary="Журнал авторизаций и действий",
    description=ADMIN_ONLY,
    responses={403: {"model": ErrorResponse, "description": "Только администратор"}},
)
async def get_audit_logs(
    _: Annotated[CurrentUser, Depends(require_admin)],
    actor_username: Optional[str] = Query(None, max_length=100),
    action_type: Optional[str] = Query(None, max_length=100),
    entity_type: Optional[str] = Query(None, max_length=50),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    rows, _total = services.list_audit_logs(
        actor_username=actor_username, action_type=action_type, entity_type=entity_type,
        limit=limit, offset=offset,
    )
    return rows
