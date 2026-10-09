"""Хендл lsm_db однопоточный: регрессия на assert_db_state.

Хендл хранит номер читателя, открытые курсоры и глубину транзакций и сверяет
их на входе в каждую операцию (`assert_db_state`, lsm_main.c:43): нарушение —
abort() в отладочной сборке и молча испорченное состояние в release. Поэтому
все операции LSMTable сериализованы (блокировка + выделенный поток), и ни одна
не оставляет открытый курсор.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from src.utils.lsm_adapter import LSMTable

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_isolated(body: str) -> subprocess.CompletedProcess:
    """Выполнить сценарий в отдельном процессе: SIGABRT от assert внутри lsm1
    унёс бы весь pytest вместо одного упавшего теста."""
    env = {
        **os.environ,
        "DEV": "1",
        "RESET": "0",
        "TESTING": "Y",
        "CONFIG_PATH": "cfg/dev.yml",
        "PYTHONPATH": str(REPO_ROOT),
    }
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(body)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        timeout=180,
    )


def _encode(value: str) -> bytes:
    return value.encode("utf-8")


def _decode(key: str, data: bytes) -> str:
    return data.decode("utf-8")


@pytest.fixture
def table(tmp_path):
    t = LSMTable(path=tmp_path / "indexed.lsm", encoder=_encode, decoder=_decode)
    try:
        yield t
    finally:
        t.close()


def test_abandoned_items_iteration_leaves_no_open_cursor(table: LSMTable[str]):
    """Брошенный на полпути обход не должен держать курсор открытым.

    Самый строгий признак — БД с открытым курсором не закрывается (lsm
    возвращает Misuse).
    """
    for i in range(50):
        table.set(f"/doc{i:03d}", "v")

    leaked = table.items()
    next(leaked)  # взяли один элемент и бросили обход; ссылку держим

    table.close()  # с открытым курсором: RuntimeError: Misuse


def test_delete_prefix_leaves_no_open_cursor(table: LSMTable[str]):
    """delete_prefix закрывает курсор до открытия транзакции."""
    for i in range(50):
        table.set(f"/doc{i:03d}", "v")
    for i in range(50):
        table.set(f"/old/doc{i:03d}", "v")

    table.delete_prefix("/old/")

    assert [k for k, _ in table.items()] == [f"/doc{i:03d}" for i in range(50)]
    table.close()


def test_parallel_access_survives_and_stays_consistent(tmp_path):
    """Одновременная работа нескольких потоков не роняет процесс.

    Связка как в POST /config: полный обход таблицы (сверка удалённых файлов)
    параллельно с записями, точечными чтениями и удалением по префиксу (папку
    убрали из индексируемых).
    """
    result = _run_isolated(f"""
        import threading
        from pathlib import Path
        from src.utils.lsm_adapter import LSMTable

        table = LSMTable(
            path=Path({str(tmp_path / "indexed.lsm")!r}),
            encoder=lambda v: v.encode(),
            decoder=lambda k, d: d.decode(),
        )
        for i in range(2000):
            table.set(f"/home/user/doc_{{i:05d}}.pdf", "x")
        for i in range(2000):
            table.set(f"/home/user/old/doc_{{i:05d}}.pdf", "y")

        stop = threading.Event()
        errors = []

        def guard(fn):
            def run():
                while not stop.is_set():
                    try:
                        fn()
                    except Exception as exc:
                        errors.append(repr(exc))
                        return
            return run

        def scan():
            for _ in table.items():
                pass

        def snapshot():
            table.keys_snapshot()

        def read():
            table.get("/home/user/doc_00001.pdf")
            table.is_in("/home/user/missing.pdf")

        def write():
            table.set("/home/user/doc_00001.pdf", "z")

        def wipe_prefix():
            table.delete_prefix("/home/user/old/")

        threads = [
            threading.Thread(target=guard(fn))
            for fn in (scan, scan, snapshot, read, read, write, wipe_prefix)
        ]
        for t in threads:
            t.start()
        stop.wait(5)
        stop.set()
        for t in threads:
            t.join(30)

        assert not errors, errors[:3]
        # Таблица осталась работоспособной, а не «выжила молча».
        assert table.get("/home/user/doc_00002.pdf") == "x"
        assert not [k for k, _ in table.items() if k.startswith("/home/user/old/")]
        table.close()
        print("OK")
    """)

    assert result.returncode == 0, (
        f"процесс упал (rc={result.returncode}): {result.stderr[-2000:]}"
    )
    assert "OK" in result.stdout
