import asyncio
import sys
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from time import monotonic

from fastapi import Depends, FastAPI
from fastapi.openapi.docs import (
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPBearer
from fastapi.staticfiles import StaticFiles

from src.indexer import serve_indexer
from src.utils import get_config, get_logger
from src.utils.openvino_encoder import OpenVINOEmbedder
from src.utils.systemd_notify import (
    READY,
    STOPPING,
    keep_alive,
    notify,
    watchdog_period,
)
from src.version import __version__

from .auth import TokenFirstMiddleware, log_auth_state, verify_token
from .loopback_guard import LoopbackOnlyMiddleware
from .router import router

lg = get_logger(__name__)


def _static_dir() -> Path | None:
    """Каталог с офлайн-статикой Swagger, если она вообще положена.

    В бандле PyInstaller корень — sys._MEIPASS, как у ресурсов в src/config.py.
    При запуске из исходников — корень репозитория по пути этого модуля, а не
    cwd, от которого считает свои ресурсы config.py: статика находится при
    любом рабочем каталоге.

    None — статики нет. Это штатно: в репозитории каталог пуст, и /docs
    берёт скрипты с CDN.
    """
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    else:
        base = Path(__file__).resolve().parents[2]
    candidate = base / "src" / "server" / "static"
    if (candidate / "swagger-ui-bundle.js").is_file() and (
        candidate / "swagger-ui.css"
    ).is_file():
        return candidate
    return None


STATIC_DIR = _static_dir()

_SWAGGER_BEARER = HTTPBearer(
    auto_error=False,
    description="Токен агента из ~/.files-search/tokens/<агент>.token",
)


@asynccontextmanager
async def app_life(app: FastAPI):
    cfg = get_config()
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=cfg.asyncio_num_workers)
    )
    if STATIC_DIR is None:
        # В закрытом контуре /docs откроется пустой — причина должна быть в логе.
        lg.warning(
            "офлайн-статика Swagger не найдена: /docs будет тянуть скрипты с "
            "cdn.jsdelivr.net и в сети без интернета откроется пустой. "
            "Как положить файлы — см. src/server/static/README.md"
        )
    # Без auth.json API закрыт всем: причина 401 должна быть в логе со старта.
    log_auth_state(cfg.lsm_root)
    # Прогрев обоих каналов инференса, чтобы первый запрос не ждал создания
    # infer request. Первое обращение к cfg.ov_model компилирует модель — это
    # почти всё время старта; без строк на INFO 40–45 с выглядят как зависание.
    started = monotonic()
    lg.info("прогрев модели OpenVINO: компиляция и два канала инференса")
    await cfg.ov_model.async_encode(["test"], role="search")
    await cfg.ov_model.async_encode(["test"], role="index")
    lg.info("прогрев завершён за %.1f c", monotonic() - started)
    # После прогрева, а не перед yield: миграции индекса идут в потоках и loop
    # не держат, а TimeoutStartSec юнита ограничивает только прогрев.
    notify(READY)
    try:
        async with _watchdog(cfg.ov_model), serve_indexer():
            yield
    finally:
        notify(STOPPING)


@asynccontextmanager
async def _watchdog(model: OpenVINOEmbedder) -> AsyncIterator[None]:
    """Пинг сторожа systemd (WatchdogSec юнита), пока инференс не завис."""
    period = watchdog_period()
    if period is None:
        yield
        return
    task = asyncio.create_task(keep_alive(period, model.inference_alive))
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(
    title="LocalRAG",
    version=__version__,
    description=(
        "Локальный индексатор и семантический поиск по файлам пользователя.\n\n"
        "Индексация стартует вместе с демоном (бесшовная поставка); остановить "
        "на сессию — `POST /api/v1/deactivate`.\n\n"
        "Адрес задают `RAG_HOST` и `RAG_PORT`, по умолчанию `127.0.0.1:8000`; "
        "в поставке `RAG_PORT=0`, и порт выбирает ядро. Фактический порт "
        "опубликован в `~/.files-search/port`.\n\n"
        "Запросы, у которых `Host` или `Origin` не loopback, отклоняются "
        "(400/403): защита от DNS rebinding и запросов со сторонних сайтов.\n\n"
        "Все ручки `/api/v1`, кроме `/health`, требуют заголовок "
        "`Authorization: Bearer <токен агента>` (в Swagger — кнопка Authorize). "
        "Токен выдаёт `scripts/register_mcp.py` в "
        "`~/.files-search/tokens/<агент>.token`; без `~/.files-search/auth.json` "
        "доступ закрыт всем (401)."
    ),
    # Без локальной статики — штатный /docs через CDN.
    docs_url=None if STATIC_DIR is not None else "/docs",
    # ReDoc выключен: офлайн-версии нет, а внешнего он тянет ещё больше
    # (скрипт, favicon, шрифты Google).
    redoc_url=None,
    openapi_url="/openapi.json",
    lifespan=app_life,
)

API_PREFIX = "/api/v1"

# Внутри LoopbackOnlyMiddleware: на чужой Host/Origin — 400/403, а не 401.
app.add_middleware(
    TokenFirstMiddleware,
    paths=frozenset(API_PREFIX + route.path for route in router.routes),
)
# Снаружи всего приложения, включая /docs и статику: чужой Host/Origin не
# доходит ни до одной ручки.
app.add_middleware(LoopbackOnlyMiddleware)

# Токен — на весь роутер: новая ручка не останется открытой по забывчивости.
# /health открыта (OPEN_PATHS в auth.py). HTTPBearer ничего не проверяет — он
# даёт Swagger кнопку Authorize.
app.include_router(
    router,
    prefix=API_PREFIX,
    dependencies=[Depends(_SWAGGER_BEARER), Depends(verify_token)],
)

if STATIC_DIR is not None:
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/docs", include_in_schema=False)
    async def swagger_ui_offline():
        return get_swagger_ui_html(
            openapi_url=app.openapi_url,
            title="%s %s — Swagger UI" % (app.title, app.version),
            # Все три ссылки локальные: одна внешняя — и в закрытом контуре
            # страница пустая.
            swagger_js_url="/static/swagger-ui-bundle.js",
            swagger_css_url="/static/swagger-ui.css",
            swagger_favicon_url="/static/favicon.png",
            oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
        )

    @app.get(app.swagger_ui_oauth2_redirect_url, include_in_schema=False)
    async def swagger_ui_redirect():
        return get_swagger_ui_oauth2_redirect_html()


# Swagger и схема живут в корне приложения, а ищут их под /api/v1 рядом с
# ручками — редиректы вместо 404.
@app.get("/api/v1/docs", include_in_schema=False)
async def docs_alias():
    return RedirectResponse("/docs")


@app.get("/api/v1/openapi.json", include_in_schema=False)
async def openapi_alias():
    return RedirectResponse("/openapi.json")
