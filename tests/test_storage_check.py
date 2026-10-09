"""Подготовка хранилища при старте (src/indexer/storage_check.py)."""

from pathlib import Path
from types import SimpleNamespace

from lsm import LSM

from src.indexer.storage_check import STATE_TABLE, remove_email_leftovers
from src.utils.lsm_adapter import LSMCollection, lsm_file

FILE_KEY = b"/home/u/report.docx"
EMAIL_KEY = b"email_INBOX:0:120"
STATE = b"x" * 12


def _storage(root: Path, *, with_email: bool = True) -> SimpleNamespace:
    """Хранилище демона: индекс файлов и, по желанию, остатки почты."""
    lance = root / "lance"
    (lance / "files.lance").mkdir(parents=True)
    (root / "ocr").mkdir()
    keys = [FILE_KEY]
    if with_email:
        (lance / "files_email.lance" / "data").mkdir(parents=True)
        (root / "emails").mkdir()
        (root / "emails.retired.1.2").mkdir()
        (root / "offsets.json").write_text("{}")
        keys.append(EMAIL_KEY)
    with LSM(str(lsm_file(root, STATE_TABLE))) as db:
        for key in keys:
            db[key] = STATE
    return SimpleNamespace(lsm_root=str(root), ld_db_path=str(lance))


def _keys(root: Path) -> list[bytes]:
    with LSM(str(lsm_file(root, STATE_TABLE))) as db:
        return list(db.keys())


def test_email_leftovers_are_removed(tmp_path):
    root = tmp_path / "files-search"
    cfg = _storage(root)

    remove_email_leftovers(cfg)

    assert not (root / "lance" / "files_email.lance").exists()
    assert not (root / "emails").exists()
    assert not (root / "emails.retired.1.2").exists()
    assert not (root / "offsets.json").exists()
    assert EMAIL_KEY not in _keys(root)


def test_file_index_survives_email_cleanup(tmp_path):
    root = tmp_path / "files-search"
    cfg = _storage(root)

    remove_email_leftovers(cfg)

    assert (root / "lance" / "files.lance").is_dir()
    assert (root / "ocr").is_dir()
    assert _keys(root) == [FILE_KEY]


def test_storage_without_email_is_untouched(tmp_path):
    root = tmp_path / "files-search"
    cfg = _storage(root, with_email=False)
    before = sorted(p.name for p in root.iterdir())

    remove_email_leftovers(cfg)

    assert sorted(p.name for p in root.iterdir()) == before
    assert _keys(root) == [FILE_KEY]


def test_fresh_storage_gets_no_lsm(tmp_path):
    """На чистой установке зачистка ничего не создаёт — LSM откроет Index."""
    root = tmp_path / "files-search"
    root.mkdir()

    remove_email_leftovers(SimpleNamespace(lsm_root=str(root), ld_db_path=str(root)))

    assert list(root.iterdir()) == []


def test_state_table_is_the_one_index_opens(config, tmp_path, monkeypatch):
    """Проверка и LSMCollection должны смотреть в один и тот же файл."""
    monkeypatch.setattr(config, "lsm_root", str(tmp_path))

    with LSMCollection() as collection:
        path = collection.indexed.path

    assert path == lsm_file(tmp_path, STATE_TABLE)
