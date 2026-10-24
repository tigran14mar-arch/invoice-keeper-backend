"""
Заглушка FastAPI для среды, где пакет не установлен.

Нужна только тестам: позволяет импортировать app/main.py и проверить состав
маршрутов там, где нет возможности поставить зависимости. Если FastAPI
установлен (обычный случай), тесты используют настоящий пакет, а этот файл не
подключается. В приложении заглушка не участвует.
"""
from __future__ import annotations

import sys
import types
from typing import Any


class _Route:
    def __init__(self, path: str, methods: set[str], endpoint: Any, kwargs: dict) -> None:
        self.path = path
        self.methods = methods
        self.endpoint = endpoint
        self.kwargs = kwargs


class HTTPException(Exception):
    def __init__(self, status_code: int, detail: Any = "", headers: dict | None = None) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.headers = headers


class _Depends:
    def __init__(self, dependency: Any = None) -> None:
        self.dependency = dependency


def Depends(dependency: Any = None) -> _Depends:  # noqa: N802
    return _Depends(dependency)


def Query(default: Any = None, **_: Any) -> Any:  # noqa: N802
    return default


class Request:
    headers: dict = {}
    cookies: dict = {}
    client = None


class Response:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs

    def set_cookie(self, **_: Any) -> None:
        return None

    def delete_cookie(self, *_: Any, **__: Any) -> None:
        return None


class JSONResponse(Response):
    pass


class RedirectResponse(Response):
    pass


class FastAPI:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.routes: list[_Route] = []
        self.exception_handlers: dict = {}
        self.middleware: list = []

    def _register(self, method: str, path: str, kwargs: dict):
        def decorator(func):
            self.routes.append(_Route(path, {method}, func, kwargs))
            return func
        return decorator

    def get(self, path: str, **kwargs: Any):
        return self._register("GET", path, kwargs)

    def post(self, path: str, **kwargs: Any):
        return self._register("POST", path, kwargs)

    def patch(self, path: str, **kwargs: Any):
        return self._register("PATCH", path, kwargs)

    def put(self, path: str, **kwargs: Any):
        return self._register("PUT", path, kwargs)

    def delete(self, path: str, **kwargs: Any):
        return self._register("DELETE", path, kwargs)

    def exception_handler(self, exc: Any):
        def decorator(func):
            self.exception_handlers[exc] = func
            return func
        return decorator

    def add_middleware(self, middleware_class: Any, **kwargs: Any) -> None:
        self.middleware.append((middleware_class, kwargs))

    def openapi(self) -> dict:
        return {"openapi": "3.1.0", "paths": {route.path: {} for route in self.routes}}


def install() -> None:
    """Ставит заглушку в sys.modules, если настоящий FastAPI недоступен."""
    try:
        import fastapi  # noqa: F401
        return
    except ImportError:
        pass

    module = types.ModuleType("fastapi")
    module.FastAPI = FastAPI
    module.Depends = Depends
    module.Query = Query
    module.Request = Request
    module.Response = Response
    module.HTTPException = HTTPException
    module.status = types.SimpleNamespace(
        HTTP_200_OK=200, HTTP_201_CREATED=201, HTTP_204_NO_CONTENT=204,
        HTTP_401_UNAUTHORIZED=401, HTTP_403_FORBIDDEN=403, HTTP_404_NOT_FOUND=404,
    )

    middleware = types.ModuleType("fastapi.middleware")
    cors = types.ModuleType("fastapi.middleware.cors")
    cors.CORSMiddleware = object
    middleware.cors = cors

    openapi_module = types.ModuleType("fastapi.openapi")
    docs = types.ModuleType("fastapi.openapi.docs")
    docs.get_swagger_ui_html = lambda **kwargs: Response(**kwargs)
    docs.get_redoc_html = lambda **kwargs: Response(**kwargs)
    openapi_module.docs = docs

    responses = types.ModuleType("fastapi.responses")
    responses.JSONResponse = JSONResponse
    responses.RedirectResponse = RedirectResponse

    module.middleware = middleware
    module.openapi = openapi_module
    module.responses = responses

    sys.modules["fastapi"] = module
    sys.modules["fastapi.middleware"] = middleware
    sys.modules["fastapi.middleware.cors"] = cors
    sys.modules["fastapi.openapi"] = openapi_module
    sys.modules["fastapi.openapi.docs"] = docs
    sys.modules["fastapi.responses"] = responses
