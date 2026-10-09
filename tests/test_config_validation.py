"""POST/GET /config: пустой список папок, окно почты, неизвестные ключи.

Плюс каталог пользовательского конфига с «~» и размерность эмбеддинга.

Хендлеры зовутся напрямую с заглушкой индекса из tests/test_config_reload.py;
422 от FastAPI проверяется на маленьком приложении с тем же роутером, без
lifespan (модель и индексатор не поднимаются).
"""

import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.config import Config
from src.indexer.utils import _get_index
from src.server.model import EmailConfig, UserConfig
from src.server.router import get_config, router, update_config
from src.utils.config_mixin import _get_config

# Фикстуры вокруг POST /config и заглушка индекса — из соседнего набора.
from tests.test_config_reload import RecordingIndex, cfg, journal  # noqa: F401


@pytest.fixture
def client(
    cfg,  # noqa: F811
    journal,  # noqa: F811
):
    """Роутер демона на приложении без lifespan: тело проверяет FastAPI."""
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[_get_config] = lambda: cfg
    app.dependency_overrides[_get_index] = lambda: RecordingIndex(journal)
    return TestClient(app)


def _deleted(calls: list) -> list[str]:
    return [c[1] for c in calls if isinstance(c, tuple) and c[0] == "delete_prefix"]


# --- пустой или null folders ------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("folders", [[], None])
async def test_empty_or_null_folders_rejected(
    cfg,  # noqa: F811
    journal,  # noqa: F811
    tmp_path,
    folders,
):
    """[] и null — 400, конфиг на диске и индекс не тронуты.

    Config.scan_paths понимает их как «настройки нет» и обходит пути из YAML:
    принятый [] снял бы весь индекс и запустил полный обход умолчания.
    """
    docs = tmp_path / "docs"
    docs.mkdir()
    fake_index = RecordingIndex(journal)
    await update_config(
        UserConfig.model_validate({"folders": [str(docs)]}), cfg, fake_index
    )
    journal.clear()
    before = cfg.user_config

    with pytest.raises(HTTPException) as exc:
        await update_config(
            UserConfig.model_validate({"folders": folders}), cfg, fake_index
        )

    assert exc.value.status_code == 400
    assert cfg.user_config == before, "конфиг на диске изменился"
    assert journal == [], f"индекс или наблюдение тронуты: {journal}"


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [None, []])
async def test_get_config_shows_paths_the_daemon_scans(
    cfg,  # noqa: F811
    tmp_path,
    monkeypatch,
    stored,
):
    """null или [] в user_conf.yml: GET /config отдаёт корни, которые обходит демон.

    По этому списку мост строит песочницу local_read_file и тело local_add_path.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(cfg.file_scan_cfg, "paths", [str(home)])
    cfg.update_user_config(UserConfig.model_validate({"folders": stored}))

    shown = (await get_config(cfg)).folders

    assert shown == [str(home)]
    assert [str(Path(p).expanduser()) for p in cfg.scan_paths] == shown


@pytest.mark.asyncio
async def test_add_folder_over_stored_null_keeps_index(
    cfg,  # noqa: F811
    journal,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    """Как local_add_path моста при folders: null в user_conf.yml.

    Мост дописывает папку к списку из GET /config. Пустой GET дал бы тело [X],
    и роутер снял бы с индекса весь корень из YAML.
    """
    home = tmp_path / "home"
    extra = home / "u" / "X"
    extra.mkdir(parents=True)
    monkeypatch.setattr(cfg.file_scan_cfg, "paths", [str(home)])
    cfg.update_user_config(UserConfig.model_validate({"folders": None}))

    current = list((await get_config(cfg)).folders or [])
    await update_config(
        UserConfig.model_validate({"folders": current + [str(extra)]}),
        cfg,
        RecordingIndex(journal),
    )

    assert _deleted(journal) == [], f"добавление папки сняло с индекса: {journal}"
    assert set(cfg.scan_paths) == {str(home), str(extra)}


# --- неизвестные ключи ------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"folder": ["/tmp"]},
        {"paths": ["/tmp"]},
        {"email": {"enabeld": True}},
        {"sberchat": {"enable": True}},
    ],
)
def test_unknown_keys_are_rejected(
    client,
    cfg,  # noqa: F811
    journal,  # noqa: F811
    body,
):
    """Опечатка в ключе — 422, а не 200 без изменений."""
    resp = client.post("/api/v1/config", json=body)

    assert resp.status_code == 422, resp.text
    assert cfg.user_config is None, "конфиг записан, хотя запрос отвергнут"
    assert journal == []


def test_client_bodies_still_accepted(
    client,
    cfg,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    """Тела, которые шлют мост, стенды НТ и круг GET → POST, по-прежнему 200."""
    docs = tmp_path / "docs"
    docs.mkdir()
    monkeypatch.setattr(cfg.file_scan_cfg, "paths", [str(docs)])
    bodies = [
        {"folders": [str(docs)]},
        {"email": {"enabled": True}},
        {"email": {"enabled": False}},
        # Пример из README.
        {"folders": [str(docs)], "email": {"enabled": True, "index_last_n_days": 90}},
        {
            "folders": [str(docs)],
            "email": {
                "enabled": False,
                "path_to_email_folder": "",
                "index_last_n_days": 90,
            },
            "sberchat": {"enabled": False},
        },
        {
            "email": {
                "enabled": False,
                "index_last_n_days": None,
                "offsets": None,
                "path_to_email_folder": "",
            },
            "folders": [str(docs)],
            "sberchat": {"enabled": False},
        },
        {"max_depth": 3},
    ]
    for body in bodies:
        resp = client.post("/api/v1/config", json=body)
        assert resp.status_code == 200, (body, resp.text)

    round_trip = client.get("/api/v1/config").json()
    resp = client.post("/api/v1/config", json=round_trip)
    assert resp.status_code == 200, resp.text


# --- окно почты -------------------------------------------------------------


@pytest.mark.parametrize("days", [0, -5])
def test_email_window_below_one_day_rejected(
    client,
    cfg,  # noqa: F811
    journal,  # noqa: F811
    days,
):
    """0 и отрицательное в POST /config — 422: весь ящик или ни одного письма."""
    resp = client.post(
        "/api/v1/config", json={"email": {"enabled": True, "index_last_n_days": days}}
    )

    assert resp.status_code == 422, resp.text
    assert cfg.user_config is None
    assert journal == []


def test_email_window_null_still_means_whole_mailbox(
    client,
    cfg,  # noqa: F811
):
    """Документированный null по-прежнему снимает окно."""
    resp = client.post(
        "/api/v1/config", json={"email": {"enabled": False, "index_last_n_days": None}}
    )

    assert resp.status_code == 200, resp.text
    assert cfg.user_config.email.index_last_n_days is None
    assert cfg.email_begin_date is None


@pytest.mark.parametrize("stored", [0, -5])
def test_stored_window_below_one_day_reads_as_default(stored):
    """0 и отрицательное из старого user_conf.yml или YAML читаются как 30.

    Отвергать их при чтении нельзя: битым счёлся бы весь user_conf.yml вместе
    со списком папок.
    """
    email = EmailConfig.model_validate({"index_last_n_days": stored})
    assert email.index_last_n_days == 30
    user = UserConfig.model_validate({"email": {"index_last_n_days": stored}})
    assert user.email.index_last_n_days == 30


@pytest.mark.parametrize("stored", [0, -5])
def test_email_begin_date_never_unbounded_or_future(
    cfg,  # noqa: F811
    monkeypatch,
    stored,
):
    monkeypatch.setattr(
        cfg,
        "email",
        EmailConfig.model_validate({"enabled": True, "index_last_n_days": stored}),
    )

    begin = cfg.email_begin_date

    assert begin is not None, "окно снято — индексировался бы весь ящик"
    expected = datetime.datetime.now() - datetime.timedelta(30)
    assert abs((begin - expected).total_seconds()) < 60


# --- «~» в data_directory ---------------------------------------------------


def test_data_directory_tilde_expanded_before_mkdir(config, tmp_path, monkeypatch):
    """«~» раскрывается до создания каталога, а не становится его именем.

    Под systemd --user рабочий каталог демона — $HOME, и рядом с настоящим
    ~/.ragsearch появлялся бы ~/~/.ragsearch.
    """
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(config, "data_directory", "~/.ragsearch-test")
    config.__dict__.pop("data_directory_path", None)
    config.__dict__.pop("user_conf_path", None)
    try:
        path = config.data_directory_path
        assert path == (home / ".ragsearch-test").resolve()
        assert path.is_dir(), "каталог конфига не создан"
        assert not (cwd / "~").exists(), "создан каталог с буквальным именем «~»"
    finally:
        config.__dict__.pop("data_directory_path", None)
        config.__dict__.pop("user_conf_path", None)


# --- размерность эмбеддинга -------------------------------------------------


def test_embed_dim_comes_from_model_output(config):
    """Размерность — по выходу модели, а не по имени её каталога.

    Под embed_dim создаётся таблица LanceDB: неугаданная размерность роняла бы
    запись векторов на первом батче.
    """
    fresh = Config()
    # Имя без подсказок эвристики («minilm», «rubert-tiny», «user2»).
    fresh.embed_model_cfg.model_path = "models/my-embedder"
    # Настоящая скомпилированная модель сессии — без второй компиляции.
    fresh.__dict__["ov_model"] = config.ov_model

    assert fresh.embed_dim == config.ov_model.encode(["проверка"]).shape[1]


def test_embed_dim_without_compiled_model_falls_back_to_name():
    """Подменённая в тестах модель без CompiledModel — размерность по имени."""
    fresh = Config()
    fresh.__dict__["ov_model"] = SimpleNamespace()

    assert fresh.embed_dim == 312
