import os

# DEV=1 — до импорта src.config: пути хранилища вычисляются при импорте,
# иначе тесты пишут индексы в реальный ~/.files-search.
os.environ.setdefault("DEV", "1")
os.environ["RESET"] = "0"  # тесты никогда не сносят хранилище
# Дублирует [tool.pytest_env]: плагина pytest-env в зависимостях нет. Без
# CONFIG_PATH kwargs конструктора Config игнорируются и тесты читают общий
# user_conf.yml.
os.environ.setdefault("CONFIG_PATH", "cfg/dev.yml")
os.environ.setdefault("TESTING", "Y")
# TestClient шлёт Host: testserver, а API принимает только loopback
# (src/server/loopback_guard.py). Тесты самой проверки переменную снимают.
os.environ.setdefault("RAG_ALLOWED_HOSTS", "testserver")

from pytest import fixture

from src.config import Config
from src.utils import set_config


@fixture(scope="session", autouse=True)
def config():
    cfg = Config()
    set_config(cfg)
    return cfg


@fixture
def user_conf_config(config, tmp_path, monkeypatch):
    """Сессионный Config с user_conf.yml во временном каталоге."""
    monkeypatch.setattr(config, "data_directory", str(tmp_path / "ragsearch"))
    _forget_user_conf_paths(config)
    yield config
    _forget_user_conf_paths(config)


def _forget_user_conf_paths(cfg: Config) -> None:
    # Пути кэшируют cached_property: без сброса тест читал бы чужой каталог.
    cfg.__dict__.pop("user_conf_path", None)
    cfg.__dict__.pop("data_directory_path", None)
