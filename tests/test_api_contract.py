"""
Проверка состава API: все эндпоинты из технического задания на месте,
адреса начинаются с /api/v1, а админские действия закрыты от обычного пользователя.

Эти тесты ловят опечатку в адресе или потерянную проверку прав раньше, чем жюри.
"""
from __future__ import annotations

import typing
import unittest

from tests import fastapi_stub, sqlite_double

sqlite_double.install()   # подменяет app.database и bcrypt
fastapi_stub.install()    # только если настоящий FastAPI не установлен

from app import main  # noqa: E402
from app.security import current_user, require_admin  # noqa: E402

# Таблицы из ТЗ (слайды 12–14): метод, адрес
REQUIRED_ENDPOINTS = [
    ("POST", "/api/v1/auth/login"),
    ("GET", "/api/v1/auth/me"),
    ("POST", "/api/v1/auth/logout"),
    ("GET", "/api/v1/recurring-payments"),
    ("POST", "/api/v1/recurring-payments"),
    ("DELETE", "/api/v1/recurring-payments/{payment_id}"),
    ("GET", "/api/v1/payment-occurrences"),
    ("GET", "/api/v1/payment-occurrences/{occurrence_id}"),
    ("PATCH", "/api/v1/payment-occurrences/{occurrence_id}"),
    ("GET", "/api/v1/payments/summary"),
    ("GET", "/api/v1/users"),
    ("POST", "/api/v1/users"),
    ("PATCH", "/api/v1/users/{user_id}"),
    ("DELETE", "/api/v1/users/{user_id}"),
    ("GET", "/api/v1/docs"),
]

ADMIN_ONLY = [
    ("POST", "/api/v1/recurring-payments"),
    ("DELETE", "/api/v1/recurring-payments/{payment_id}"),
    ("GET", "/api/v1/users"),
    ("POST", "/api/v1/users"),
    ("PATCH", "/api/v1/users/{user_id}"),
    ("DELETE", "/api/v1/users/{user_id}"),
    ("PATCH", "/api/v1/settings"),
    ("POST", "/api/v1/reminders/run"),
    ("GET", "/api/v1/audit-logs"),
    ("PUT", "/api/v1/budgets"),
    ("DELETE", "/api/v1/budgets/{budget_id}"),
]

PUBLIC = {"/", "/api/v1/docs", "/api/v1/redoc", "/api/v1/health", "/api/v1/auth/login", "/api/v1/openapi.json"}


def routes() -> dict[tuple[str, str], object]:
    found = {}
    for route in main.app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        endpoint = getattr(route, "endpoint", None)
        if path and endpoint:
            for method in methods:
                if method not in {"HEAD", "OPTIONS"}:
                    found[(method, path)] = endpoint
    return found


def dependencies_of(endpoint) -> list:
    """Зависимости, объявленные через Annotated[..., Depends(...)]."""
    result = []
    try:
        hints = typing.get_type_hints(endpoint, include_extras=True)
    except Exception:  # noqa: BLE001
        return result
    for hint in hints.values():
        for meta in getattr(hint, "__metadata__", ()):
            dependency = getattr(meta, "dependency", None)
            if dependency is not None:
                result.append(dependency)
    return result


class ApiContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.routes = routes()

    def test_all_required_endpoints_exist(self):
        missing = [item for item in REQUIRED_ENDPOINTS if item not in self.routes]
        self.assertEqual(missing, [], f"Нет обязательных эндпоинтов из ТЗ: {missing}")

    def test_every_endpoint_uses_api_v1_prefix(self):
        wrong = [path for _, path in self.routes if not path.startswith("/api/v1") and path != "/"]
        self.assertEqual(wrong, [], f"Адреса вне /api/v1: {wrong}")

    def test_admin_endpoints_require_admin(self):
        for method, path in ADMIN_ONLY:
            endpoint = self.routes.get((method, path))
            self.assertIsNotNone(endpoint, f"Нет эндпоинта {method} {path}")
            self.assertIn(require_admin, dependencies_of(endpoint), f"{method} {path} не закрыт от пользователя")

    def test_data_endpoints_require_authorization(self):
        """Без авторизации панель и данные недоступны (критерий приёмки)."""
        for (method, path), endpoint in self.routes.items():
            if path in PUBLIC:
                continue
            deps = dependencies_of(endpoint)
            self.assertTrue(
                current_user in deps or require_admin in deps,
                f"{method} {path} доступен без авторизации",
            )

    def test_openapi_is_served_from_api_v1(self):
        config = getattr(main.app, "kwargs", {})
        self.assertEqual(getattr(main.app, "openapi_url", config.get("openapi_url")), "/api/v1/openapi.json")
        self.assertIsNone(getattr(main.app, "docs_url", config.get("docs_url")), "Swagger должен быть на /api/v1/docs")


if __name__ == "__main__":
    unittest.main(verbosity=2)
