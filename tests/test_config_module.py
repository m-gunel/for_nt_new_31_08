"""src/config.py: лёгкий импорт и отсутствие мёртвого кода загрузки моделей."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.config import Config, EmbeddingModelConfig

ROOT = Path(__file__).resolve().parents[1]


def test_config_import_does_not_build_server():
    """Импорт конфига не поднимает FastAPI-приложение, индексатор и парсеры."""
    code = (
        "import sys, src.config\n"
        "heavy = [m for m in ('src.server', 'src.server.app', 'src.indexer',"
        " 'src.parsers') if m in sys.modules]\n"
        "print(heavy)\n"
        "sys.exit(1 if heavy else 0)\n"
    )
    env = {**os.environ, "DEV": "1", "RESET": "0", "CONFIG_PATH": "cfg/dev.yml"}
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_user_config_models_are_one_class():
    """Роутер, конфиг и тесты видят один и тот же класс, а не копии."""
    import src.config as config_module
    from src.common import user_config
    from src.server import model

    assert model.UserConfig is user_config.UserConfig
    assert model.SberchatConfig is user_config.SberchatConfig
    assert model.EmailConfig is user_config.EmailConfig is config_module.EmailConfig


@pytest.mark.parametrize(
    "name",
    [
        "openai",
        "gigachat",
        "gigachat_models",
        "embedder",
        "st_model",
        "embed_query",
        "_load_transformers_model",
        "_load_sentense_transformers_model",
    ],
)
def test_config_has_no_dead_model_loaders(name):
    assert not hasattr(Config, name)


def test_no_disabled_tls_verification_in_src():
    offenders = [
        str(p.relative_to(ROOT))
        for p in (ROOT / "src").rglob("*.py")
        if "verify_ssl_certs=False" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_idle_check_module_removed():
    assert importlib.util.find_spec("src.indexer.idle_check") is None


def test_embed_passages_fake_still_works():
    cfg = Config()
    cfg.embed_model_cfg = EmbeddingModelConfig(model_type="fake")
    vectors = cfg.embed_passages(["а", "б"])
    assert len(vectors) == 2 and all(len(v) == 1024 for v in vectors)
