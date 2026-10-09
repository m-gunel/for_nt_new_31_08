"""Отказ запросам, у которых Host или Origin — не loopback.

Первый рубеж против браузера пользователя: страница стороннего сайта шлёт POST
без тела (CSRF: /reset, /deactivate), а через DNS rebinding ещё и читает
ответы /search и /read_file. Host и Origin браузер выставляет сам, страница их
не подделает. Мост на urllib и curl Origin не шлют и проходят по loopback-Host.

Отказ 400/403 приходит раньше проверки токена. Процесс другой учётки на том же
АРМ эту проверку проходит — его отсекает токен агента (src/server/auth.py).
"""

import ipaddress
import os
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# Имена через запятую, которые принимаются в Host и Origin сверх loopback:
# для демона, явно открытого в сеть (RAG_HOST и LQRAG_EXPOSE=1), и тестового
# клиента.
ALLOWED_HOSTS_ENV = "RAG_ALLOWED_HOSTS"

_HOST_DETAIL = (
    "запрос отклонён: в заголовке Host не loopback-адрес. API принимает "
    "127.0.0.1, localhost и ::1, другие имена — из RAG_ALLOWED_HOSTS"
)
_ORIGIN_DETAIL = (
    "запрос отклонён: Origin не loopback — запросы со страниц сторонних "
    "сайтов к API не допускаются"
)


def _extra_hosts() -> set[str]:
    # На каждый запрос: дёшево, и тесты меняют переменную на лету.
    raw = os.environ.get(ALLOWED_HOSTS_ENV, "")
    return {name.strip().lower() for name in raw.split(",") if name.strip()}


def _is_loopback(name: str) -> bool:
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def host_allowed(netloc: str | None) -> bool:
    """«хост[:порт]» из Host или Origin — loopback или имя из RAG_ALLOWED_HOSTS."""
    # «@» — userinfo: urlsplit счёл бы хостом то, что после него.
    if not netloc or "@" in netloc:
        return False
    try:
        parts = urlsplit("//" + netloc)
        # Свойство port разбирает порт: мусор или вне диапазона — ValueError.
        _ = parts.port
    except ValueError:
        return False
    # Путь или запрос в Host — не адрес.
    if parts.netloc != netloc or not parts.hostname:
        return False
    return _is_loopback(parts.hostname) or parts.hostname in _extra_hosts()


def origin_allowed(origin: str) -> bool:
    """Источник страницы — http(s) с разрешённого хоста; «null» не проходит."""
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and host_allowed(parts.netloc)


class LoopbackOnlyMiddleware:
    """Отказ до роутинга: ни ручка, ни её зависимости не вызываются."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            origin = headers.get("origin")
            response = None
            if not host_allowed(headers.get("host")):
                response = JSONResponse({"detail": _HOST_DETAIL}, status_code=400)
            elif origin is not None and not origin_allowed(origin):
                response = JSONResponse({"detail": _ORIGIN_DETAIL}, status_code=403)
            if response is not None:
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
