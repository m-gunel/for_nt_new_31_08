"""Каталоги данных: RESET=1 по путям из конфига, свой user_conf.yml под DEV=1,
права 0700 на индексе и пользовательском конфиге."""

import ast
import os
import stat
from pathlib import Path

import pytest

import src.config as config_module
from src.config import Config

ROOT = Path(__file__).resolve().parents[1]


def _cfg(**paths) -> Config:
    """Config с путями хранилища в tmp.

    Поля проставляются после конструктора: без CONFIG_PATH аргументы
    Config(...) игнорируются. Кэш путей сбрасывается.
    """
    cfg = Config()
    for name, value in paths.items():
        setattr(cfg, name, str(value))
    cfg.__dict__.pop("data_directory_path", None)
    cfg.__dict__.pop("user_conf_path", None)
    return cfg


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.skipif(os.environ.get("DEV") != "1", reason="проверка режима DEV=1")
def test_dev_mode_keeps_user_conf_inside_dev_storage():
    """DEV=1 не читает и не пишет user_conf.yml установленного продукта."""
    default = Path(Config.model_fields["data_directory"].default)
    assert default.resolve().is_relative_to(config_module._root.resolve())
    assert default.resolve() != Path("~/.ragsearch").expanduser().resolve()


def test_reset_storage_removes_configured_paths(tmp_path, caplog):
    lsm_root = tmp_path / "files-search"
    lance = tmp_path / "elsewhere" / "lance"
    data = tmp_path / "ragsearch"
    (lsm_root / "ocr").mkdir(parents=True)
    (lsm_root / "ocr" / "page.json").write_text("{}", encoding="utf-8")
    lance.mkdir(parents=True)
    (lance / "files.lance").write_text("x", encoding="utf-8")
    data.mkdir()
    (data / "user_conf.yml").write_text("folders: [/tmp]\n", encoding="utf-8")
    cfg = _cfg(lsm_root=lsm_root, ld_db_path=lance, data_directory=data)

    with caplog.at_level("INFO", logger="app.config"):
        config_module.reset_storage(cfg)

    assert lsm_root.is_dir() and not any(lsm_root.iterdir())
    assert not lance.exists()
    assert not (data / "user_conf.yml").exists()
    assert str(lsm_root) in caplog.text


def test_reset_storage_keeps_agent_tokens(tmp_path: Path) -> None:
    """auth.json, tokens/ и надгробия — доступ мостов к API, а не индекс."""
    lsm_root = tmp_path / "files-search"
    tokens = lsm_root / config_module.TOKENS_DIR_NAME
    tokens.mkdir(parents=True)
    (tokens / "gigacode.token").write_text("секрет", encoding="utf-8")
    auth_file = lsm_root / config_module.AUTH_FILE_NAME
    auth_file.write_text('{"gigacode": "хэш"}', encoding="utf-8")
    (lsm_root / config_module.WITHDRAWN_FILE_NAME).write_text("хэш\n", encoding="utf-8")
    (lsm_root / "ocr").mkdir()
    (lsm_root / "ocr" / "page.json").write_text("{}", encoding="utf-8")
    (lsm_root / "port").write_text("8000", encoding="utf-8")
    cfg = _cfg(
        lsm_root=lsm_root,
        ld_db_path=tmp_path / "lance",
        data_directory=tmp_path / "ragsearch",
    )

    config_module.reset_storage(cfg)

    assert sorted(p.name for p in lsm_root.iterdir()) == sorted(
        [
            config_module.AUTH_FILE_NAME,
            config_module.TOKENS_DIR_NAME,
            config_module.WITHDRAWN_FILE_NAME,
        ]
    )
    assert auth_file.read_text(encoding="utf-8") == '{"gigacode": "хэш"}'
    assert (tokens / "gigacode.token").read_text(encoding="utf-8") == "секрет"


def test_reset_storage_never_removes_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "keep.txt").write_text("личный файл", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    cfg = _cfg(lsm_root=home, ld_db_path=home / "lance", data_directory=tmp_path / "d")

    config_module.reset_storage(cfg)

    assert (home / "keep.txt").exists()


def test_protect_storage_closes_existing_dirs(tmp_path):
    lsm_root = tmp_path / "files-search"
    data = tmp_path / "ragsearch"
    for d in (lsm_root, data):
        d.mkdir()
        os.chmod(d, 0o755)
    cfg = _cfg(lsm_root=lsm_root, data_directory=data)

    config_module.protect_storage(cfg)

    assert _mode(lsm_root) == 0o700
    assert _mode(data) == 0o700


def test_protect_storage_creates_missing_dirs_private(tmp_path):
    lsm_root = tmp_path / "a" / "files-search"
    data = tmp_path / "ragsearch"
    cfg = _cfg(lsm_root=lsm_root, data_directory=data)

    config_module.protect_storage(cfg)

    assert _mode(lsm_root) == 0o700
    assert _mode(data) == 0o700


def test_protect_storage_leaves_home_alone(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    os.chmod(home, 0o755)
    monkeypatch.setenv("HOME", str(home))
    cfg = _cfg(lsm_root=tmp_path / "s", data_directory=home)

    config_module.protect_storage(cfg)

    assert _mode(home) == 0o755


def test_protect_storage_failure_does_not_stop_daemon(tmp_path, monkeypatch, caplog):
    def deny(*args, **kwargs):
        raise PermissionError("EPERM")

    monkeypatch.setattr(config_module.os, "chmod", deny)
    cfg = _cfg(lsm_root=tmp_path / "s", data_directory=tmp_path / "d")

    with caplog.at_level("WARNING", logger="app.config"):
        config_module.protect_storage(cfg)

    assert "0700" in caplog.text


def _first_stmt_calling(tree: ast.Module, name: str) -> int:
    for i, stmt in enumerate(tree.body):
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call):
                func = node.func
                called = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else getattr(func, "id", None)
                )
                if called == name:
                    return i
    raise AssertionError(f"в main.py нет вызова {name}")


def test_main_resets_and_protects_storage_after_logging_setup():
    """Сброс — после настройки логирования (иначе строка о нём теряется) и
    до файлового лога, который лежит внутри сносимого lsm_root."""
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    order = [
        _first_stmt_calling(tree, name)
        for name in (
            "set_config",
            "basicConfig",
            "reset_storage_if_requested",
            "protect_storage",
            "_attach_file_log",
        )
    ]
    assert order == sorted(order) and len(set(order)) == len(order)
