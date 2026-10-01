import os

# DEV=1 должен быть выставлен ДО импорта src.config: пути хранилища
# вычисляются на уровне модуля. Без этого прогон тестов пишет индексы
# в реальный ~/.files-search пользователя.
os.environ.setdefault("DEV", "1")
# Страховка: тесты никогда не должны сносить хранилище.
os.environ["RESET"] = "0"
# Здесь, а не только в [tool.pytest_env]: плагина pytest-env в зависимостях
# нет. Без CONFIG_PATH из цепочки настроек выпадает init_settings, kwargs
# конструктора Config игнорируются, и тесты читают реальный ~/.ragsearch.
os.environ.setdefault("CONFIG_PATH", "cfg/dev.yml")
os.environ.setdefault("TESTING", "Y")

from pytest import fixture

from src.config import Config
from src.utils import set_config


@fixture(scope="session", autouse=True)
def config():
    cfg = Config()
    set_config(cfg)
    return cfg
