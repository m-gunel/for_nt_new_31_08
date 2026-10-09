"""Настройка из окружения: в конфиг попадают только переменные с префиксом RAG_.

Все проверки строят Config с собственным data_directory: от него считается путь
к user_conf.yml, и без изоляции тест подхватит общий user_conf.yml
вместе с CONFIG_PATH из conftest.
"""

import pytest

from src.config import Config


def config_in_tmp(tmp_path) -> Config:
    """Config, гарантированно изолированный от реального ~/.ragsearch.

    Поле проставляется после конструктора: без CONFIG_PATH
    `settings_customise_sources` выбрасывает init_settings из цепочки, и
    `Config(data_directory=...)` молча вернул бы дефолт.
    """
    cfg = Config()
    cfg.data_directory = str(tmp_path)
    # cached_property уже могли посчитаться от прежнего data_directory
    cfg.__dict__.pop("data_directory_path", None)
    cfg.__dict__.pop("user_conf_path", None)
    return cfg


@pytest.mark.parametrize(
    ("name", "value"),
    [
        # Ставят git и почтовые утилиты на многих Linux-машинах.
        ("EMAIL", "ivanov@sberbank.ru"),
        # Совпадает с полем logging_level, а значение для него недопустимо.
        ("LOGGING_LEVEL", "громко"),
    ],
)
def test_stray_env_does_not_break_config(
    tmp_path, monkeypatch: pytest.MonkeyPatch, name: str, value: str
):
    """Переменная оболочки без префикса не попадает в конфиг.

    Недопустимое значение уронило бы демон на старте с ValidationError.
    """
    monkeypatch.setenv(name, value)

    cfg = config_in_tmp(tmp_path)

    assert value not in str(cfg.model_dump())


def test_prefixed_env_still_configures(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Настройка через окружение работает под префиксом RAG_."""
    # Не DEBUG: его уже задаёт cfg/dev.yml, и проверка прошла бы без окружения.
    monkeypatch.setenv("RAG_APP_LOGGING_LEVEL", "WARNING")

    cfg = config_in_tmp(tmp_path)

    assert cfg.app_logging_level == "WARNING"


def test_isolated_config_never_reads_real_user_conf(tmp_path, monkeypatch):
    """Сторож изоляции: и без CONFIG_PATH тест не читает настоящий ~/.ragsearch."""
    monkeypatch.delenv("CONFIG_PATH", raising=False)

    cfg = config_in_tmp(tmp_path)

    assert cfg.data_directory == str(tmp_path)
    assert str(cfg.user_conf_path).startswith(str(tmp_path))
