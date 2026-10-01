"""Почта выключена — значит индексатор не должен её ни искать, ни читать.

Все проверки строят Config с собственным data_directory: от него считается путь
к user_conf.yml, и без изоляции тест подхватит личный ~/.ragsearch разработчика
вместе с CONFIG_PATH из pytest-env.
"""

import pytest

from src.config import Config, EmailConfig
from src.indexer.scanners.email_scanner import EmailsScanner


def isolated_config(tmp_path, **overrides) -> Config:
    """Config, гарантированно изолированный от реального ~/.ragsearch.

    Поля проставляются ПОСЛЕ конструктора намеренно: без CONFIG_PATH
    `settings_customise_sources` выбрасывает init_settings из цепочки
    источников, и `Config(data_directory=...)` молча возвращает дефолт —
    тест начинал читать настоящий конфиг пользователя вместо tmp_path.
    """
    cfg = Config()
    cfg.data_directory = str(tmp_path)
    for name, value in overrides.items():
        setattr(cfg, name, value)
    # cached_property уже могли посчитаться от прежнего data_directory
    cfg.__dict__.pop("data_directory_path", None)
    cfg.__dict__.pop("user_conf_path", None)
    return cfg


def test_stray_email_env_does_not_break_config(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Переменная EMAIL из оболочки не должна попадать в конфиг.

    Она есть в окружении многих Linux-машин (её ставят git и почтовые утилиты),
    а поле конфига ждёт объект EmailConfig — без префикса демон падал на старте
    с ValidationError.
    """
    monkeypatch.setenv("EMAIL", "ivanov@sberbank.ru")
    cfg = isolated_config(tmp_path)  # не должно бросить
    assert cfg.email is None or isinstance(cfg.email, EmailConfig)


def test_prefixed_env_still_configures(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Настройка через окружение не потеряна — она переехала под префикс."""
    monkeypatch.setenv("RAG_APP_LOGGING_LEVEL", "DEBUG")
    assert isolated_config(tmp_path).app_logging_level == "DEBUG"


def test_isolated_config_never_reads_real_user_conf(tmp_path, monkeypatch):
    """Сторож изоляции: тесты не должны читать настоящий ~/.ragsearch.

    Без CONFIG_PATH (например, когда не установлен pytest-env) kwargs
    конструктора Config игнорируются, и без явной установки полей тест
    подхватывал реальный конфиг пользователя — падая или проходя в
    зависимости от того, что у него на машине.
    """
    monkeypatch.delenv("CONFIG_PATH", raising=False)
    cfg = isolated_config(tmp_path)
    assert cfg.data_directory == str(tmp_path)
    assert str(cfg.user_conf_path).startswith(str(tmp_path))


def test_email_disabled_when_not_configured(tmp_path):
    """Отсутствие секции email трактуется как «выключено», а не «не задано»."""
    cfg = isolated_config(tmp_path, email=None)
    assert cfg.email_config is None
    assert cfg.email_enabled is False


@pytest.mark.asyncio
async def test_email_scanner_yields_nothing_when_disabled(tmp_path):
    """Сканер не обходит почтовые папки при выключенной почте.

    email_path ведёт в несуществующий каталог: не сработай гейт, обход упал бы.
    """
    scanner = EmailsScanner(
        queue_path=tmp_path / "emails",
        email_path=tmp_path / "нет-такого-каталога",
        folders=["INBOX"],
        lsm_checker=None,  # type: ignore[arg-type]  # до обращения дело не дойдёт
    )
    assert [meta async for meta in scanner.preindex()] == []


def test_mail_root_not_searched_when_disabled(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """При выключенной почте корень Thunderbird не ищется.

    Поиск — рекурсивный обход домашнего каталога, на старте демона он не нужен.
    """
    import src.indexer.indexer as indexer_module
    import src.thunderbird.find_files as find_files_module

    calls: list[object] = []
    monkeypatch.setattr(
        find_files_module,
        "find_email_root",
        lambda home=None: calls.append(home) or None,
    )
    cfg = isolated_config(tmp_path, email=None)
    assert cfg.email_enabled is False
    indexer_module.Index(cfg)
    assert calls == [], "поиск почтового корня не должен вызываться"
