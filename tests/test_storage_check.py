"""Подготовка хранилища при старте (src/indexer/storage_check.py)."""

import dataclasses
import shutil
from pathlib import Path
from types import SimpleNamespace

import lancedb
import pyarrow as pa
import pytest
from lsm import LSM

from src.common import chunk_with_meta_schema
from src.indexer import storage_check
from src.indexer.storage_check import (
    CORRUPT_DIR,
    INDEX_FORMAT,
    STAMP_FILE,
    STATE_TABLE,
    ensure_index_usable,
    expected_stamp,
    quarantine,
    read_stamp,
    remove_email_leftovers,
    write_stamp,
)
from src.utils.lsm_adapter import LSMCollection, lsm_file
from src.utils.openvino_encoder import MODEL_IR, TOKENIZER_IR, model_fingerprint

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


# --- проверка индекса при старте ---------------------------------------------

DIM = 4


def _model(path: Path, weights: bytes = b"v1") -> Path:
    """Каталог модели с IR модели и токенизатора — всё, что входит в отпечаток."""
    for ir in (MODEL_IR, TOKENIZER_IR):
        xml = path / ir
        xml.parent.mkdir(parents=True, exist_ok=True)
        xml.write_bytes(b"<net/>")
        xml.with_suffix(".bin").write_bytes(weights)
    return path


def _index_cfg(tmp_path: Path, model: Path, embed_dim: int = DIM) -> SimpleNamespace:
    root = tmp_path / "files-search"
    root.mkdir(exist_ok=True)
    return SimpleNamespace(
        lsm_root=str(root),
        ld_db_path=str(root / "lance"),
        ld_files_table="files",
        embed_model_cfg=SimpleNamespace(model_path=str(model)),
        embed_dim=embed_dim,
    )


def _table(
    cfg: SimpleNamespace,
    paths: tuple[str, ...] = ("/home/u/a.txt",),
    dim: int = DIM,
    *,
    legacy: bool = False,
) -> None:
    """Таблица файлов: текущей схемы или, при legacy, схемы до 1.0.8 (без kind)."""
    if legacy:
        fields = [
            f for f in chunk_with_meta_schema if f.name in ("idx", "text", "path")
        ]
        schema = pa.schema(fields)
    else:
        schema = chunk_with_meta_schema
    schema = schema.append(pa.field("vector", pa.list_(pa.float32(), dim)))
    rows = [
        {"idx": 0, "text": "текст", "path": p, "vector": [0.1] * dim} for p in paths
    ]
    db = lancedb.connect(cfg.ld_db_path)
    db.create_table(cfg.ld_files_table, pa.Table.from_pylist(rows, schema=schema))


def _remember(cfg: SimpleNamespace, *keys: bytes) -> None:
    with LSM(str(lsm_file(cfg.lsm_root, STATE_TABLE))) as db:
        for key in keys:
            db[key] = STATE


def _index_kept(cfg: SimpleNamespace) -> bool:
    return (Path(cfg.ld_db_path) / "files.lance").exists() and lsm_file(
        cfg.lsm_root, STATE_TABLE
    ).exists()


def _index_wiped(cfg: SimpleNamespace) -> bool:
    root = Path(cfg.lsm_root)
    return not (Path(cfg.ld_db_path) / "files.lance").exists() and not list(
        root.glob(f"{STATE_TABLE}.lsm*")
    )


def test_fresh_storage_gets_stamp(tmp_path):
    cfg = _index_cfg(tmp_path, _model(tmp_path / "rubert"))

    ensure_index_usable(cfg)

    stamp = read_stamp(Path(cfg.lsm_root))
    assert stamp == expected_stamp(cfg)
    assert (stamp.format, stamp.model, stamp.embed_dim) == (INDEX_FORMAT, "rubert", DIM)


def test_current_index_without_stamp_is_kept(tmp_path):
    """Индекс 1.0.8–1.0.10 файла версии не знает — его принимаем без пересборки."""
    cfg = _index_cfg(tmp_path, _model(tmp_path / "rubert"))
    _table(cfg)
    _remember(cfg, FILE_KEY)

    ensure_index_usable(cfg)

    assert _index_kept(cfg)
    assert read_stamp(Path(cfg.lsm_root)) == expected_stamp(cfg)


def test_model_change_rebuilds_index(tmp_path):
    model = _model(tmp_path / "rubert")
    cfg = _index_cfg(tmp_path, model)
    _table(cfg)
    _remember(cfg, FILE_KEY)
    ensure_index_usable(cfg)
    _model(model, weights=b"v2")

    ensure_index_usable(cfg)

    assert _index_wiped(cfg)
    assert read_stamp(Path(cfg.lsm_root)) == expected_stamp(cfg)


def test_renamed_model_dir_keeps_index(tmp_path):
    cfg = _index_cfg(tmp_path, _model(tmp_path / "rubert"))
    _table(cfg)
    _remember(cfg, FILE_KEY)
    ensure_index_usable(cfg)
    cfg.embed_model_cfg.model_path = str(_model(tmp_path / "rubert-copy"))

    ensure_index_usable(cfg)

    assert _index_kept(cfg)


def test_format_change_rebuilds_index(tmp_path):
    cfg = _index_cfg(tmp_path, _model(tmp_path / "rubert"))
    _table(cfg)
    _remember(cfg, FILE_KEY)
    stale = dataclasses.replace(expected_stamp(cfg), format=INDEX_FORMAT - 1)
    write_stamp(Path(cfg.lsm_root), stale)

    ensure_index_usable(cfg)

    assert _index_wiped(cfg)


@pytest.mark.parametrize(
    "table_kwargs",
    [
        {"legacy": True},
        {"dim": DIM * 2},
        {"paths": ("/home/u/a.txt", "email_INBOX:0:1:body")},
    ],
    ids=["schema-1.0.7", "other-dim", "email-rows"],
)
def test_incompatible_index_is_rebuilt(tmp_path, table_kwargs):
    cfg = _index_cfg(tmp_path, _model(tmp_path / "rubert"))
    _table(cfg, **table_kwargs)
    _remember(cfg, FILE_KEY)

    ensure_index_usable(cfg)

    assert _index_wiped(cfg)
    assert not (Path(cfg.lsm_root) / CORRUPT_DIR).exists(), "старый индекс — не порча"


def test_state_without_table_is_reset(tmp_path):
    """LSM помнит файлы, а таблицы нет — иначе они в индекс не вернутся."""
    cfg = _index_cfg(tmp_path, _model(tmp_path / "rubert"))
    _remember(cfg, FILE_KEY)

    ensure_index_usable(cfg)

    assert _index_wiped(cfg)


def test_empty_table_with_remembered_files_is_kept(tmp_path):
    """Файлы без текста LSM помнит, а строк не дают: это не повод пересобирать."""
    cfg = _index_cfg(tmp_path, _model(tmp_path / "rubert"))
    _table(cfg, paths=())
    _remember(cfg, FILE_KEY)

    ensure_index_usable(cfg)

    assert _index_kept(cfg)


def test_unreadable_table_goes_to_corrupt(tmp_path):
    cfg = _index_cfg(tmp_path, _model(tmp_path / "rubert"))
    _table(cfg)
    _remember(cfg, FILE_KEY)
    shutil.rmtree(Path(cfg.ld_db_path) / "files.lance" / "_versions")

    ensure_index_usable(cfg)

    assert _index_wiped(cfg)
    (copy,) = (Path(cfg.lsm_root) / CORRUPT_DIR).iterdir()
    assert (copy / "files.lance").is_dir()


def test_unreadable_state_goes_to_corrupt(tmp_path, monkeypatch):
    cfg = _index_cfg(tmp_path, _model(tmp_path / "rubert"))
    _table(cfg)
    _remember(cfg, FILE_KEY)

    def broken_lsm(path: str):
        raise RuntimeError("LSM_CORRUPT")

    monkeypatch.setattr(storage_check, "LSM", broken_lsm)

    ensure_index_usable(cfg)

    assert _index_wiped(cfg)
    (copy,) = (Path(cfg.lsm_root) / CORRUPT_DIR).iterdir()
    assert (copy / f"{STATE_TABLE}.lsm").is_file()


def test_only_last_corrupt_copy_is_kept(tmp_path):
    root = tmp_path / "corrupt"
    for name in ("first", "second"):
        damaged = tmp_path / name
        damaged.mkdir()
        quarantine([damaged], root)

    (copy,) = root.iterdir()
    assert [p.name for p in copy.iterdir()] == ["second"]


def test_rebuild_keeps_settings_ocr_and_logs(tmp_path):
    model = _model(tmp_path / "rubert")
    cfg = _index_cfg(tmp_path, model)
    root = Path(cfg.lsm_root)
    for kept in ("ocr", "logs", "ragsearch"):
        (root / kept).mkdir()
    (root / "port").write_text("8000")
    # При DEV=1 user_conf.yml лежит внутри каталога данных индекса.
    (root / "ragsearch" / "user_conf.yml").write_text("folders: [/home/u]\n")
    (root / "files.retired.1.2").mkdir()
    _table(cfg)
    _remember(cfg, FILE_KEY)
    ensure_index_usable(cfg)
    _model(model, weights=b"v2")

    ensure_index_usable(cfg)

    assert _index_wiped(cfg)
    assert {p.name for p in root.iterdir()} >= {"ocr", "logs", "port", STAMP_FILE}
    assert (root / "ragsearch" / "user_conf.yml").is_file()
    assert not (root / "files.retired.1.2").exists(), "хвост очереди — часть индекса"


def test_unreadable_stamp_counts_as_missing(tmp_path):
    cfg = _index_cfg(tmp_path, _model(tmp_path / "rubert"))
    _table(cfg)
    _remember(cfg, FILE_KEY)
    (Path(cfg.lsm_root) / STAMP_FILE).write_text("{обрезан")

    ensure_index_usable(cfg)

    assert _index_kept(cfg)
    assert read_stamp(Path(cfg.lsm_root)) == expected_stamp(cfg)


def test_fingerprint_depends_on_weights_not_on_dir_name(tmp_path):
    first = model_fingerprint(_model(tmp_path / "a"))

    assert model_fingerprint(_model(tmp_path / "b")) == first
    assert model_fingerprint(_model(tmp_path / "c", weights=b"v2")) != first
