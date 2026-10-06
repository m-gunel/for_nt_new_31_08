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

from .router import router

lg = get_logger(__name__)


def _static_dir() -> Path | None:
    """Каталог с офлайн-статикой Swagger, если она вообще положена.

    Путь резолвится так же, как остальные ресурсы проекта (см. src/config.py):
    в бандле PyInstaller корнем служит sys._MEIPASS, при запуске из исходников —
    корень репозитория.

    Возвращает None, если статики нет. Это штатная ситуация, а не ошибка:
    каталог в репозитории намеренно пустой, и без файлов страница /docs просто
    работает по-старому, через CDN.
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
    # get_event_loop() внутри работающего цикла устарел — берём текущий явно.
    get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=cfg.asyncio_num_workers)
    )
    if STATIC_DIR is None:
        # Не ошибка, но знать об этом надо: в закрытом контуре страница
        # откроется пустой, и без этой строки причина ниоткуда не видна.
        lg.warning(
            "офлайн-статика Swagger не найдена: /docs будет тянуть скрипты с "
            "cdn.jsdelivr.net и в сети без интернета откроется пустой. "
            "Как положить файлы — см. src/server/static/README.md"
        )
    # Прогреваем оба канала инференса, чтобы первый живой запрос не платил
    # за создание infer-request'а. Обращение к cfg.ov_model — это cached_property,
    # то есть именно здесь происходит компиляция модели OpenVINO, и она
    # занимает почти всё время старта.
    #
    # Замер нужен не ради красоты: раньше единственной отметкой о прогреве был
    # lg.debug в src/config.py, невидимый на штатном INFO, и 40-45 секунд старта
    # выглядели в логе как зависание без единой строки.
    started = monotonic()
    lg.info("прогрев модели OpenVINO: компиляция и два канала инференса")
    await cfg.ov_model.async_encode(["test"], role="search")
    await cfg.ov_model.async_encode(["test"], role="index")
    if cfg.reranker is not None:
        await cfg.reranker.async_score("test", ["test"])
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
        "Порт назначает ядро (`RAG_PORT=0`); фактический опубликован в "
        "`~/.files-search/port`."
    ),
    # Свой роут /docs регистрируем только когда есть локальная статика:
    # иначе оставляем штатный, чтобы поведение не менялось там, где CDN доступен.
    docs_url=None if STATIC_DIR is not None else "/docs",
    # ReDoc выключен намеренно. Офлайн-варианта для него мы не держим, а тянет
    # он вчетверо больше внешнего: redoc.standalone.js, favicon и шрифты Google.
    # Одной работающей страницы документации достаточно.
    redoc_url=None,
    openapi_url="/openapi.json",
    lifespan=app_life,
)

app.include_router(router, prefix="/api/v1")

if STATIC_DIR is not None:
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/docs", include_in_schema=False)
    async def swagger_ui_offline():
        return get_swagger_ui_html(
            openapi_url=app.openapi_url,
            title="%s %s — Swagger UI" % (app.title, app.version),
            # Все три ссылки локальные. Достаточно упустить одну, чтобы
            # страница снова полезла в интернет и снова оказалась пустой.
            swagger_js_url="/static/swagger-ui-bundle.js",
            swagger_css_url="/static/swagger-ui.css",
            swagger_favicon_url="/static/favicon.png",
            oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
        )

    @app.get(app.swagger_ui_oauth2_redirect_url, include_in_schema=False)
    async def swagger_ui_redirect():
        return get_swagger_ui_oauth2_redirect_html()


# Алиасы под префиксом роутера. Swagger и схема живут в КОРНЕ приложения —
# include_router(prefix=...) на них не распространяется. Но все рабочие ручки
# лежат под /api/v1, поэтому рука тянется туда же, и /api/v1/docs отдаёт 404
# ровно в тот момент, когда человек пытается разобраться, что вообще есть в API.
@app.get("/api/v1/docs", include_in_schema=False)
async def docs_alias():
    return RedirectResponse("/docs")


@app.get("/api/v1/openapi.json", include_in_schema=False)
async def openapi_alias():
    return RedirectResponse("/openapi.json")
