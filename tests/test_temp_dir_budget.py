"""Учёт временных файлов, выдаваемых через POST /get_file_path.

Письмо ограничено 48 МиБ, а окно выдач — 32 каталога, то есть до полутора
гигабайт. На АРМ каталог временных файлов обычно смонтирован как tmpfs, и это
занимает оперативную память, а не диск.
"""

from pathlib import Path

import src.server.router as router_module
from src.server.router import _register_temp_dir


def _reset_registry() -> None:
    router_module._temp_dirs.clear()
    router_module._temp_dirs_bytes = 0


def _make_dir(tmp_path: Path, name: str, size: int) -> Path:
    d = tmp_path / name
    d.mkdir()
    (d / "payload.bin").write_bytes(b"\0" * min(size, 1024))
    return d


def test_total_size_stays_under_budget(tmp_path):
    """Суммарный объём выдач держится под лимитом, а не только их число."""
    _reset_registry()
    chunk = router_module._MAX_TEMP_BYTES // 4

    dirs = [_make_dir(tmp_path, f"d{i}", chunk) for i in range(10)]
    for d in dirs:
        _register_temp_dir(d, chunk)

    assert router_module._temp_dirs_bytes <= router_module._MAX_TEMP_BYTES
    assert len(router_module._temp_dirs) < 10, "старые выдачи должны вытесняться"
    # вытесняются самые старые, свежие остаются на диске
    assert dirs[-1].exists()
    assert not dirs[0].exists()
    _reset_registry()


def test_last_handout_survives_even_if_oversized(tmp_path):
    """Только что выданный файл не удаляем: путь уже уехал клиенту в ответе."""
    _reset_registry()
    huge = router_module._MAX_TEMP_BYTES * 2
    d = _make_dir(tmp_path, "huge", 1024)

    _register_temp_dir(d, huge)

    assert d.exists()
    assert len(router_module._temp_dirs) == 1
    _reset_registry()


def test_count_limit_still_applies(tmp_path):
    """Мелкие выдачи вытесняются по числу — прежнее поведение сохранено."""
    _reset_registry()
    dirs = [
        _make_dir(tmp_path, f"s{i}", 16)
        for i in range(router_module._MAX_TEMP_DIRS + 5)
    ]
    for d in dirs:
        _register_temp_dir(d, 16)

    assert len(router_module._temp_dirs) == router_module._MAX_TEMP_DIRS
    assert not dirs[0].exists()
    _reset_registry()
