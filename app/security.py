"""
Безопасность: пароли, сессии, роли и журнал действий.

Здесь живёт всё, что отвечает на вопросы «кто ты?» и «можно ли тебе это?»:
 * хеширование и проверка паролей (bcrypt);
 * создание, проверка и отзыв сессий (таблица user_sessions);
 * зависимости FastAPI: current_user (любой вошедший) и require_admin (только админ);
 * запись действий в audit_logs.

Администратор задаётся в .env (Слайд 6, 9). При старте он синхронизируется в таблицу
users с флагом is_env_admin = TRUE, чтобы у сессий был внешний ключ, а API запрещал
изменять или удалять эту учётную запись.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

import bcrypt
from fastapi import Depends, HTTPException, Request, status

from app.config import settings
from app.database import execute, query_one, transaction

log = logging.getLogger("invoice_keeper.security")

SESSION_TOKEN_BYTES = 32


# --------------------------------------------------------------------------- пароли

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, password_hash: Optional[str]) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # В базе лежит строка, не похожая на bcrypt-хеш (например, демо-заглушка из seed-данных).
        return False


def _token_fingerprint(token: str) -> str:
    """В базе хранится необратимый отпечаток токена, а не сам токен:
    даже при утечке дампа базы чужие сессии нельзя подделать."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- текущий пользователь

@dataclass(frozen=True)
class CurrentUser:
    id: Optional[int]
    username: str
    role: str
    full_name: Optional[str] = None
    is_env_admin: bool = False
    session_token_hash: str = ""

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


# --------------------------------------------------------------------------- администратор из .env

def sync_env_admin() -> None:
    """Приводит учётную запись администратора из .env в соответствие с базой."""
    username = settings.admin_username
    password_hash = hash_password(settings.admin_password)
    with transaction() as conn:
        row = query_one("SELECT id, is_env_admin FROM users WHERE username = %s", (username,), conn=conn)
        if row is None:
            execute(
                "INSERT INTO users (username, password_hash, full_name, role, is_env_admin, is_active) "
                "VALUES (%s, %s, %s, 'admin', TRUE, TRUE)",
                (username, password_hash, settings.admin_full_name),
                conn=conn,
            )
            log.info("Администратор из .env создан в базе: %s", username)
        else:
            execute(
                "UPDATE users SET password_hash = %s, full_name = %s, role = 'admin', "
                "is_env_admin = TRUE, is_active = TRUE WHERE id = %s",
                (password_hash, settings.admin_full_name, row["id"]),
                conn=conn,
            )
            log.info("Администратор из .env синхронизирован: %s", username)
        # Защита от «двух админов»: прежние env-админы с другим логином теряют флаг и права.
        execute(
            "UPDATE users SET is_env_admin = FALSE, role = 'user' "
            "WHERE is_env_admin = TRUE AND username <> %s",
            (username,),
            conn=conn,
        )


# --------------------------------------------------------------------------- вход и сессии

def authenticate(username: str, password: str) -> Optional[dict]:
    """Проверяет логин и пароль. Для админа из .env источник истины — .env."""
    user = query_one(
        "SELECT id, username, password_hash, full_name, role, is_env_admin, is_active, telegram_user_id "
        "FROM users WHERE username = %s",
        (username,),
    )
    is_env_admin_login = hmac.compare_digest(username, settings.admin_username)

    if is_env_admin_login:
        if not hmac.compare_digest(password, settings.admin_password):
            return None
        if user is None:
            # База могла быть пересоздана после старта — восстанавливаем запись администратора.
            sync_env_admin()
            user = query_one(
                "SELECT id, username, password_hash, full_name, role, is_env_admin, is_active, telegram_user_id "
                "FROM users WHERE username = %s",
                (username,),
            )
        return user

    if user is None or not user["is_active"]:
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    return user


def create_session(user: dict) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    expires_at = datetime.now(settings.tzinfo).replace(tzinfo=None) + timedelta(hours=settings.session_ttl_hours)
    execute(
        "INSERT INTO user_sessions (session_token, user_id, username, role, expires_at) "
        "VALUES (%s, %s, %s, %s, %s)",
        (_token_fingerprint(token), user["id"], user["username"], user["role"], expires_at),
    )
    return token, expires_at


def revoke_session(token_hash: str) -> None:
    execute("UPDATE user_sessions SET is_revoked = TRUE WHERE session_token = %s", (token_hash,))


def revoke_sessions_of_user(user_id: int, *, conn: Any = None) -> None:
    execute("UPDATE user_sessions SET is_revoked = TRUE WHERE user_id = %s", (user_id,), conn=conn)


def cleanup_expired_sessions() -> int:
    rowcount, _ = execute("DELETE FROM user_sessions WHERE expires_at < NOW() - INTERVAL 7 DAY")
    return rowcount


def _extract_token(request: Request) -> Optional[str]:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    cookie = request.cookies.get(settings.cookie_name)
    return cookie.strip() if cookie else None


_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Требуется авторизация",
    headers={"WWW-Authenticate": "Bearer"},
)


async def current_user(request: Request) -> CurrentUser:
    """Зависимость FastAPI: пускает только вошедшего пользователя (Критерий приёмки: без авторизации данных нет)."""
    token = _extract_token(request)
    if not token:
        raise _UNAUTHORIZED
    token_hash = _token_fingerprint(token)
    row = query_one(
        "SELECT s.session_token, s.user_id, s.username, s.role, "
        "       u.full_name, u.is_env_admin, u.is_active "
        "FROM user_sessions s LEFT JOIN users u ON u.id = s.user_id "
        "WHERE s.session_token = %s AND s.is_revoked = FALSE AND s.expires_at > NOW()",
        (token_hash,),
    )
    if row is None:
        raise _UNAUTHORIZED
    if row["user_id"] is not None and not row["is_active"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Учётная запись отключена")
    return CurrentUser(
        id=row["user_id"],
        username=row["username"],
        role=row["role"],
        full_name=row["full_name"],
        is_env_admin=bool(row["is_env_admin"]),
        session_token_hash=token_hash,
    )


async def require_admin(user: CurrentUser = Depends(current_user)) -> CurrentUser:
    """Зависимость FastAPI: только администратор (Слайд 9 — ограничения действуют и в API)."""
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Недостаточно прав: действие доступно только администратору",
        )
    return user


# --------------------------------------------------------------------------- журнал действий (бонус, Слайд 16)

def log_action(
    actor_username: str,
    actor_role: str,
    action_type: str,
    *,
    entity_type: Optional[str] = None,
    entity_id: Optional[int] = None,
    details: Optional[dict] = None,
    ip_address: Optional[str] = None,
    conn: Any = None,
) -> None:
    """Пишет действие в audit_logs. Журнал не должен ломать основную операцию."""
    try:
        execute(
            "INSERT INTO audit_logs (actor_username, actor_role, action_type, entity_type, entity_id, details, ip_address) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                actor_username,
                actor_role,
                action_type,
                entity_type,
                entity_id,
                json.dumps(details, ensure_ascii=False, default=str) if details else None,
                ip_address,
            ),
            conn=conn,
        )
    except Exception:  # noqa: BLE001 — журнал не критичен
        log.exception("Не удалось записать действие %s в журнал", action_type)


def client_ip(request: Request) -> Optional[str]:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return request.client.host[:45] if request.client else None
