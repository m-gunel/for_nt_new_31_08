"""Устойчивость user_conf.yml: порча файла не роняет конфиг, запись атомарна."""

from pytest import fixture

from src.common.user_config import UserConfig
from src.config import Config


@fixture
def cfg(user_conf_config: Config, tmp_path, monkeypatch) -> Config:
    """Плюс известные папки из YAML — на них откатывается битый user_conf.yml."""
    monkeypatch.setattr(
        user_conf_config.file_scan_cfg, "paths", [str(tmp_path / "docs")]
    )
    return user_conf_config


def test_broken_user_conf_falls_back_to_yaml(cfg: Config):
    """Обрезанный user_conf.yml не должен ронять API и обход файлов."""
    cfg.user_conf_path.write_text("folders: [unclosed\n  bad: : yaml")

    assert cfg.user_config is None
    # GET /config и обход берут папки из YAML — демон продолжает работать.
    assert cfg.user_config_with_default.folders == cfg.file_scan_cfg.paths
    assert cfg.scan_paths == cfg.file_scan_cfg.paths


def test_user_conf_with_email_section_keeps_folders(cfg: Config):
    """user_conf.yml прежних версий с секцией email читается без потерь.

    Сочти его битым из-за лишнего ключа, демон молча перешёл бы на папки из YAML
    и потерял выбор пользователя.
    """
    cfg.user_conf_path.write_text(
        "folders: [/home/u/docs]\n"
        "email:\n"
        "  enabled: true\n"
        "  path_to_email_folder: /home/u/.thunderbird\n"
    )

    assert cfg.scan_paths == ["/home/u/docs"]


def test_user_conf_written_atomically(cfg: Config):
    cfg.update_user_config(UserConfig(folders=["/tmp/x"]))

    assert cfg.user_conf_path.exists()
    assert not cfg.user_conf_path.with_suffix(".yml.tmp").exists()
    assert cfg.user_config is not None
    assert cfg.user_config.folders == ["/tmp/x"]
