"""Почта не должна быть способна уронить индексацию файлов.

Папка, которой нет локально (например, «Входящие» не синхронизированы), не
должна обрывать scan_all FileNotFoundError'ом до обработки уже собранных в
очередь файлов.

Все почтовые данные — синтетические mbox из tests/mbox_fixture.py, реальный
Thunderbird не нужен.
"""

import pytest
import pytest_asyncio as pa_fixture
from pytest import fixture

from src.config import Config, EmailConfig
from src.indexer.indexer import Index
from src.indexer.scanners.email_scanner import EmailsScanner
from src.thunderbird.find_files import find_email_root, list_email_folders
from src.thunderbird.lazy_iter_mbox import _generate_toc_lazy
from tests.mbox_fixture import default_inbox_messages, make_profile


@fixture
def mail_root(tmp_path):
    """Синтетический почтовый корень: INBOX с пятью письмами."""
    return make_profile(tmp_path / "tb-profile", {"INBOX": default_inbox_messages()})


@fixture
def email_config(config: Config, mail_root, tmp_path, monkeypatch):
    """Конфиг с включённой почтой, явным корнем и «висячей» папкой в offsets.

    offsets ссылается и на существующий INBOX, и на папку, файла которой на
    диске нет.
    """
    monkeypatch.setattr(config, "lsm_root", str(tmp_path / "storage"))
    monkeypatch.setattr(config, "lsm_path", str(tmp_path / "storage" / "lsm.ldb"))
    monkeypatch.setattr(config, "ld_db_path", str(tmp_path / "storage" / "lance"))
    monkeypatch.setattr(config, "index_refresh_delay", 0.05)
    # data_directory во временный каталог: user_conf.yml не существует,
    # значит email_config берётся из поля config.email ниже.
    monkeypatch.setattr(config, "data_directory", str(tmp_path / "ragsearch"))
    config.__dict__.pop("user_conf_path", None)
    config.__dict__.pop("data_directory_path", None)
    monkeypatch.setattr(
        config,
        "email",
        EmailConfig(
            enabled=True,
            path_to_email_folder=str(mail_root),
            offsets={"INBOX": 0, "Не-синхронизированные-Входящие": 0},
        ),
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "заметка.txt").write_text(
        "Локальный файл про регламент отпусков", encoding="utf-8"
    )
    monkeypatch.setattr(config.file_scan_cfg, "paths", [str(docs)])
    config.__dict__.pop("lance", None)
    yield config
    config.__dict__.pop("lance", None)
    config.__dict__.pop("user_conf_path", None)
    config.__dict__.pop("data_directory_path", None)


@pa_fixture.fixture
async def index(email_config: Config):
    idx = Index(email_config)
    async with idx:
        try:
            yield idx
        finally:
            if idx._index_task is not None:
                idx._index_task.cancel()
            await idx.stop_emails_polling()


def test_find_email_root_without_sent_folder(tmp_path):
    """Корень находится по INBOX, даже когда «Отправленных» нет локально."""
    expected = make_profile(
        tmp_path / ".thunderbird" / "abc.default",
        {"INBOX": default_inbox_messages()},
    )
    assert find_email_root(tmp_path) == expected


def test_find_email_root_missing_profile(tmp_path):
    assert find_email_root(tmp_path) is None


def test_find_email_root_prefers_corporate_host(tmp_path):
    profile = tmp_path / ".thunderbird" / "abc.default"
    make_profile(
        profile, {"INBOX": default_inbox_messages()}, imap_host="imap.other.ru"
    )
    corp = make_profile(
        profile, {"INBOX": default_inbox_messages()}, imap_host="imap.sberbank.ru"
    )
    assert find_email_root(tmp_path) == corp


_PROFILES_INI = """\
[Install4F96D1932A9F858E]
Default={active}
Locked=1

[Profile1]
Name=default
IsRelative=1
Path={legacy}
Default=1

[Profile0]
Name=default-release
IsRelative=1
Path={active}

[General]
StartWithLastProfile=1
Version=2
"""


def _two_profiles(tmp_path, **hosts):
    """Два профиля с почтой: aaaa.default и zzzz.default-release."""
    tb = tmp_path / ".thunderbird"
    roots = {
        name: make_profile(
            tb / name,
            {"INBOX": default_inbox_messages()},
            imap_host=hosts.get(name.split(".")[0], "imap.sberbank.ru"),
        )
        for name in ("aaaa.default", "zzzz.default-release")
    }
    return tb, roots


@pytest.mark.parametrize("active", ["aaaa.default", "zzzz.default-release"])
def test_find_email_root_prefers_installed_profile(tmp_path, active):
    """Профиль, который открывает Thunderbird, выигрывает у прежнего с тем же
    хостом, в каком бы порядке файловая система ни отдавала каталоги."""
    tb, roots = _two_profiles(tmp_path)
    legacy = next(name for name in roots if name != active)
    (tb / "profiles.ini").write_text(_PROFILES_INI.format(active=active, legacy=legacy))
    assert find_email_root(tmp_path) == roots[active]


def test_find_email_root_reads_installs_ini(tmp_path):
    tb, roots = _two_profiles(tmp_path)
    (tb / "installs.ini").write_text(
        "[4F96D1932A9F858E]\nDefault=zzzz.default-release\nLocked=1\n"
    )
    assert find_email_root(tmp_path) == roots["zzzz.default-release"]


def test_find_email_root_uses_default_flag(tmp_path):
    """Без секций [Install…] — профиль с Default=1."""
    tb, roots = _two_profiles(tmp_path)
    (tb / "profiles.ini").write_text(
        "[Profile0]\nName=new\nIsRelative=1\nPath=zzzz.default-release\nDefault=1\n"
        "\n[Profile1]\nName=old\nIsRelative=1\nPath=aaaa.default\n"
    )
    assert find_email_root(tmp_path) == roots["zzzz.default-release"]


@pytest.mark.parametrize("ini", [None, "не ini вовсе"])
def test_find_email_root_without_usable_ini_is_deterministic(tmp_path, ini):
    """Нет profiles.ini или он битый — профиль по имени каталога."""
    tb, roots = _two_profiles(tmp_path)
    if ini is not None:
        (tb / "profiles.ini").write_text(ini)
    assert find_email_root(tmp_path) == roots["aaaa.default"]


def test_corporate_host_outranks_installed_profile(tmp_path):
    """Корпоративный хост по-прежнему главнее выбора профиля."""
    tb, roots = _two_profiles(tmp_path, zzzz="imap.other.ru")
    (tb / "profiles.ini").write_text(
        _PROFILES_INI.format(active="zzzz.default-release", legacy="aaaa.default")
    )
    assert find_email_root(tmp_path) == roots["aaaa.default"]


def test_profile_choice_is_logged_once(tmp_path, monkeypatch, caplog):
    import src.thunderbird.find_files as ff

    monkeypatch.setattr(ff, "_reported_root", None)
    _two_profiles(tmp_path)
    with caplog.at_level("INFO", logger="app.email"):
        first = find_email_root(tmp_path)
        find_email_root(tmp_path)
    lines = [r for r in caplog.records if "почта берётся из" in r.getMessage()]
    assert len(lines) == 1 and str(first) in lines[0].getMessage()


def test_list_email_folders_requires_pair(mail_root):
    # .msf без mbox рядом не считается папкой
    (mail_root / "Черновики.msf").write_bytes(b"")
    assert list_email_folders(mail_root) == ["INBOX"]


def test_toc_of_missing_file_is_empty():
    assert list(_generate_toc_lazy("/нет/такого/mbox")) == []


def test_toc_offset_beyond_eof_is_empty(mail_root, caplog):
    """Сохранённый offset за концом файла — след компакции: пусто + warning."""
    mbox = mail_root / "INBOX"
    with caplog.at_level("WARNING", logger="app.thunderbird.lazy_iter_mbox"):
        toc = list(_generate_toc_lazy(str(mbox), start_offset=10**9))
    assert toc == []
    assert any("compacted" in r.message for r in caplog.records)


def test_email_root_prefers_explicit_path(email_config: Config, mail_root):
    assert email_config.email_root == mail_root


def test_email_root_missing_explicit_path(email_config: Config, monkeypatch):
    """Несуществующий явный путь — None, без фолбэка на обход Thunderbird."""
    import src.thunderbird.find_files as ff

    monkeypatch.setattr(
        ff,
        "find_email_root",
        lambda home=None: pytest.fail("автопоиск не должен вызываться"),
    )
    monkeypatch.setattr(
        email_config,
        "email",
        EmailConfig(enabled=True, path_to_email_folder="/нет/такого"),
    )
    assert email_config.email_root is None


@pytest.mark.asyncio
async def test_preindex_skips_missing_folder(email_config: Config, tmp_path):
    """Висячая папка в offsets пропускается, существующая обходится."""
    scanner = EmailsScanner(
        queue_path=tmp_path / "storage" / "emails",
        email_path=email_config.email_root,
        folders=list_email_folders(email_config.email_root),
        lsm_checker=None,  # type: ignore[arg-type]  # preindex его не трогает
    )
    metas = [m async for m in scanner.preindex()]
    # 5 писем в INBOX, одно без Date пропускается; висячая папка — ноль,
    # но и ни одного исключения.
    assert len(metas) == 4
    assert all(m.path.startswith("email_INBOX:") for m in metas)


@pytest.mark.asyncio
async def test_missing_mbox_does_not_break_file_indexing(index: Index):
    """Висячая почтовая папка не срывает индексацию файлов."""
    await index.scan_all()

    paths = {
        row["path"]
        for store in index.cfg.lance
        if store.has_table
        for row in store.table.search().limit(100).to_list()
    }
    assert any(p.endswith("заметка.txt") for p in paths), (
        "файл должен быть проиндексирован несмотря на висячую почтовую папку"
    )
    # Письма из существующего INBOX тоже дошли до индекса.
    assert any(p.startswith("email_INBOX:") for p in paths)
    # Счётчик не залип: статус вернулся к idle.
    status = await index.status()
    assert status.files_to_index == 0
    assert status.is_indexing is False
