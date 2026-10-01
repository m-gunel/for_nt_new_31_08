"""Юнит-тесты почтовых ручек роутера — без подъёма всего приложения.

Хендлеры вызываются напрямую с ручной подстановкой зависимостей: полный
lifespan (OpenVINO + LanceDB + watcher) для этих проверок не нужен.
"""

import pytest
from fastapi import HTTPException

from src.config import Config, EmailConfig
from src.server.model import RequestFilePath, SberchatConfig, UserConfig
from src.server.router import _merge_user_config, get_file_path, update_config
from tests.mbox_fixture import (
    default_inbox_messages,
    make_profile,
)


@pytest.fixture
def email_config(config: Config, tmp_path, monkeypatch):
    mail_root = make_profile(
        tmp_path / "tb-profile", {"INBOX": default_inbox_messages()}
    )
    monkeypatch.setattr(config, "data_directory", str(tmp_path / "ragsearch"))
    config.__dict__.pop("user_conf_path", None)
    config.__dict__.pop("data_directory_path", None)
    monkeypatch.setattr(
        config,
        "email",
        EmailConfig(enabled=True, path_to_email_folder=str(mail_root)),
    )
    yield config
    config.__dict__.pop("user_conf_path", None)
    config.__dict__.pop("data_directory_path", None)


def _email_key(cfg: Config, msg_id: str, suffix: str) -> str:
    """Собрать ключ email_INBOX:<start>:<stop>:<suffix> по Message-ID."""
    from src.thunderbird.lazy_iter_mbox import iter_emails_as_metadata

    (file, folder), *_ = cfg.email_files
    for _start, meta in iter_emails_as_metadata(str(file), prefix=f"email_{folder}:"):
        if meta is None:
            continue
        start, stop = meta.path.split(":")[1:3]
        with open(file, "rb") as f:
            f.seek(int(start))
            data = f.read(int(stop) - int(start))
        if msg_id.encode() in data:
            return f"{meta.path}:{suffix}"
    raise AssertionError(f"письмо {msg_id} не найдено в фикстуре")


# ------------------------------------------------------------ get_file_path -


@pytest.mark.asyncio
async def test_get_file_path_returns_body(email_config: Config):
    """Регресс на вечный 404: ключ email_INBOX:... должен находить mbox.

    Раньше словарь папок строился по голым именам ("INBOX"), а искалось
    "email_INBOX:" — ручка не работала никогда.
    """
    key = _email_key(email_config, "plain-1@fixture", "body")
    resp = await get_file_path(RequestFilePath(absolute_path=key), email_config)
    assert resp.absolute_temp_path.endswith(".eml")
    with open(resp.absolute_temp_path, "rb") as f:
        assert b"plain-1@fixture" in f.read()


@pytest.mark.asyncio
async def test_get_file_path_returns_attachment(email_config: Config):
    key = _email_key(email_config, "attach-1@fixture", "договор.txt")
    resp = await get_file_path(RequestFilePath(absolute_path=key), email_config)
    with open(resp.absolute_temp_path, "rb") as f:
        assert "Текст договора".encode() in f.read()


@pytest.mark.asyncio
async def test_get_file_path_rejects_negative_length(email_config: Config):
    """stop <= start — 400, а не чтение всего mbox в память."""
    with pytest.raises(HTTPException) as e:
        await get_file_path(
            RequestFilePath(absolute_path="email_INBOX:500:100:body"),
            email_config,
        )
    assert e.value.status_code == 400


@pytest.mark.asyncio
async def test_get_file_path_unknown_folder_404(email_config: Config):
    with pytest.raises(HTTPException) as e:
        await get_file_path(
            RequestFilePath(absolute_path="email_Ghost:0:100:body"),
            email_config,
        )
    assert e.value.status_code == 404


# ---------------------------------------------------------- merge конфига ---


def _current() -> UserConfig:
    return UserConfig(
        folders=["/home/u/docs"],
        email=EmailConfig(
            enabled=True,
            path_to_email_folder="/mail/root",
            index_last_n_days=30,
            offsets={"INBOX": 123},
        ),
    )


def test_merge_folders_only_keeps_email():
    """Регресс: POST {"folders": [...]} не должен сбрасывать почту в дефолт."""
    incoming = UserConfig.model_validate({"folders": ["/home/u/other"]})
    merged = _merge_user_config(_current(), incoming)
    assert merged.folders == ["/home/u/other"]
    assert merged.email.enabled is True
    assert merged.email.offsets == {"INBOX": 123}
    assert merged.email.index_last_n_days == 30


def test_merge_partial_email_keeps_rest():
    incoming = UserConfig.model_validate({"email": {"enabled": False}})
    merged = _merge_user_config(_current(), incoming)
    assert merged.email.enabled is False
    assert merged.email.path_to_email_folder == "/mail/root"
    assert merged.folders == ["/home/u/docs"]


def test_merge_full_replacement_still_works():
    incoming = UserConfig(
        folders=["/a"],
        email=EmailConfig(enabled=False),
        sberchat=SberchatConfig(enabled=True),
    )
    merged = _merge_user_config(_current(), incoming)
    assert merged.email.enabled is False
    assert merged.sberchat.enabled is True


# ------------------------------------------------- update_config: переходы --


class FakeIndex:
    """Протокол Index, достаточный для update_config; пишет журнал вызовов."""

    def __init__(self):
        self.calls: list = []
        self.emails_scanner = None
        # update_config сверяется с ним, чтобы не запускать сканирование на
        # деактивированном индексе (БТ §5.4). True — как у демона с
        # автозапуском, то есть прежнее поведение этих тестов.
        self.active = True

    async def stop_index(self):
        self.calls.append("stop_index")

    async def start_reindex(self):
        self.calls.append("start_reindex")

    async def delete_prefix(self, prefix):
        self.calls.append(("delete_prefix", prefix))

    async def drop_email_index(self):
        self.calls.append("drop_email_index")

    async def start_emails_polling(self, immediate=False):
        self.calls.append(("start_emails_polling", immediate))

    async def stop_emails_polling(self):
        self.calls.append("stop_emails_polling")


@pytest.fixture
def patched_watcher(monkeypatch):
    import src.server.router as router_module

    calls: list = []

    async def fake_stop():
        calls.append("stop_watcher")

    async def fake_restart():
        calls.append("restart_watcher")

    monkeypatch.setattr(router_module, "stop_watcher", fake_stop)
    monkeypatch.setattr(router_module, "restart_watcher", fake_restart)
    return calls


@pytest.mark.asyncio
async def test_enable_email_starts_polling(email_config, patched_watcher, monkeypatch):
    """Включение почты через API запускает поллинг сразу, без рестарта."""
    # стартуем с выключенной почты
    monkeypatch.setattr(email_config, "email", EmailConfig(enabled=False))
    index = FakeIndex()
    await update_config(
        UserConfig.model_validate({"email": {"enabled": True}}),
        email_config,
        index,
    )
    assert ("start_emails_polling", True) in index.calls
    saved = email_config.user_config
    assert saved is not None and saved.email.enabled is True


@pytest.mark.asyncio
async def test_disable_email_clears_index(email_config, patched_watcher):
    index = FakeIndex()
    await update_config(
        UserConfig.model_validate({"email": {"enabled": False}}),
        email_config,
        index,
    )
    assert "stop_emails_polling" in index.calls
    assert "drop_email_index" in index.calls


@pytest.mark.asyncio
async def test_folders_update_does_not_touch_email(
    email_config, patched_watcher, tmp_path
):
    """Смена папок не трогает ни поллинг, ни почтовый индекс."""
    docs = tmp_path / "docs"
    docs.mkdir()
    index = FakeIndex()
    await update_config(
        UserConfig.model_validate({"folders": [str(docs)]}),
        email_config,
        index,
    )
    email_calls = [
        c
        for c in index.calls
        if c in ("stop_emails_polling",)
        or (isinstance(c, tuple) and c[0] in ("start_emails_polling",))
        or c == "drop_email_index"
    ]
    assert email_calls == []
    saved = email_config.user_config
    assert saved is not None and saved.email.enabled is True


# ------------------------------------------------ update_config: валидация --


@pytest.mark.asyncio
async def test_config_rejects_missing_folder(email_config, patched_watcher, tmp_path):
    """Несуществующая папка отклоняется, и конфиг на диске не меняется.

    Регресс на боевой случай: Swagger по умолчанию шлёт folders: ["string"],
    API отвечал 200, путь сохранялся — и watcher падал на inotify целиком,
    переставая отслеживать вообще всё.
    """
    index = FakeIndex()
    before = email_config.user_config

    with pytest.raises(HTTPException) as exc:
        await update_config(
            UserConfig.model_validate({"folders": ["string"]}),
            email_config,
            index,
        )

    assert exc.value.status_code == 400
    assert "string" in str(exc.value.detail)
    assert email_config.user_config == before
    assert index.calls == [], "переиндексация не должна была стартовать"


@pytest.mark.asyncio
async def test_config_accepts_existing_folder(email_config, patched_watcher, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    index = FakeIndex()

    response = await update_config(
        UserConfig.model_validate({"folders": [str(docs)]}),
        email_config,
        index,
    )

    assert response.status_code == 200
    saved = email_config.user_config
    assert saved is not None and saved.folders == [str(docs)]


@pytest.mark.asyncio
async def test_disable_email_cleans_index_even_without_transition(
    email_config, patched_watcher, monkeypatch
):
    """Явное выключение чистит индекс, даже если почта уже числилась выключенной.

    Регресс на боевой случай: пользователь правит user_conf.yml руками (или
    удаляет его), демон читает «почта выключена», и последующий honest-запрос
    на выключение не удалял уже проиндексированные письма — поиск продолжал их
    возвращать, и объяснить это было нечем.
    """
    monkeypatch.setattr(email_config, "email", EmailConfig(enabled=False))
    index = FakeIndex()

    await update_config(
        UserConfig.model_validate({"email": {"enabled": False}}),
        email_config,
        index,
    )

    assert "drop_email_index" in index.calls


@pytest.mark.asyncio
async def test_unrelated_update_does_not_touch_email_index(
    email_config, patched_watcher, monkeypatch, tmp_path
):
    """Смена папок при выключенной почте не должна дёргать чистку писем."""
    monkeypatch.setattr(email_config, "email", EmailConfig(enabled=False))
    docs = tmp_path / "docs"
    docs.mkdir()
    index = FakeIndex()

    await update_config(
        UserConfig.model_validate({"folders": [str(docs)]}),
        email_config,
        index,
    )

    assert "drop_email_index" not in index.calls
