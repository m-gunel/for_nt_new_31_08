import json
import os
import secrets

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

from src.config import AUTH_FILE_NAME, Config
from src.utils import set_config

TEST_AGENT = "test-agent"


@fixture(scope="session", autouse=True)
def config():
    cfg = Config()
    # Реранкер (bge, ~0,55 ГБ) прогревался бы в каждом TestClient(app) с lifespan.
    # Переранжирование проверяют tests/test_reranker.py: на подставной модели и на
    # настоящей из models/reranker.
    cfg.reranker_model_cfg.enabled = False
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


@fixture
def agent_auth(config, tmp_path, monkeypatch) -> dict[str, str]:
    """Заголовок Authorization агента, зарегистрированного в tmp lsm_root.

    Для тестов, бьющих в настоящее приложение: все ручки /api/v1, кроме
    /health, требуют токен из <lsm_root>/auth.json.
    """
    # Не на уровне модуля: src.server тянет индексатор и OpenVINO.
    from src.server.auth import sha256_hex

    token = secrets.token_urlsafe(32)
    root = tmp_path / "auth-root"
    root.mkdir(mode=0o700)
    auth_file = root / AUTH_FILE_NAME
    auth_file.write_text(json.dumps({TEST_AGENT: sha256_hex(token)}), encoding="utf-8")
    auth_file.chmod(0o600)
    monkeypatch.setattr(config, "lsm_root", str(root))
    return {"Authorization": "Bearer " + token}
