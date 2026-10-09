import sys
from asyncio import get_running_loop
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from time import monotonic

from fastapi import FastAPI
from fastapi.openapi.docs import (
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from src.indexer import serve_indexer
from src.utils import get_config, get_logger
from src.version import __version__

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
    if (candidate / "swagger-ui-bundle.js").is_file() and (candidate / "swagger-ui.css").is_file():
        return candidate
    return None


STATIC_DIR = _static_dir()


@asynccontextmanager
async def app_life(app: FastAPI):
    cfg = get_config()
    get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=cfg.asyncio_num_workers)
    )
    if STATIC_DIR is None:
        # В закрытом контуре /docs откроется пустой — причина должна быть в логе.
        lg.warning(
            "офлайн-статика Swagger не найдена: /docs будет тянуть скрипты с "
            "cdn.jsdelivr.net и в сети без интернета откроется пустой. "
            "Как положить файлы — см. src/server/static/README.md"
        )
    # Прогрев обоих каналов инференса, чтобы первый запрос не ждал создания
    # infer request. Первое обращение к cfg.ov_model компилирует модель — это
    # почти всё время старта; без строк на INFO 40–45 с выглядят как зависание.
    started = monotonic()
    lg.info("прогрев модели OpenVINO: компиляция и два канала инференса")
    await cfg.ov_model.async_encode(["test"], role="search")
    await cfg.ov_model.async_encode(["test"], role="index")
    lg.info("прогрев завершён за %.1f c", monotonic() - started)
    async with serve_indexer():
        yield


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
        "(400/403): защита от DNS rebinding и запросов со сторонних сайтов."
    ),
    # Без локальной статики — штатный /docs через CDN.
    docs_url=None if STATIC_DIR is not None else "/docs",
    # ReDoc выключен: офлайн-версии нет, а внешнего он тянет ещё больше
    # (скрипт, favicon, шрифты Google).
    redoc_url=None,
    openapi_url="/openapi.json",
    lifespan=app_life,
)

# Снаружи всего приложения, включая /docs и статику: чужой Host/Origin не
# доходит ни до одной ручки.
app.add_middleware(LoopbackOnlyMiddleware)

app.include_router(router, prefix="/api/v1")

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
