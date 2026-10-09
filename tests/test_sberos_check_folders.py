"""sberos_check.py выбирает тот же почтовый корень и видит те же папки, что демон.

Скрипт самодостаточный (только stdlib) и src не импортирует, поэтому отбор
корня и список папок в нём продублированы — тест держит их в согласии с
src/thunderbird/find_files.py.
"""

import importlib.util
from pathlib import Path

import pytest

from src.thunderbird.find_files import find_email_root, list_email_folders
from tests.mbox_fixture import (
    default_inbox_messages,
    make_plain_email,
    make_profile,
    write_mbox,
)

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sberos_check.py"


@pytest.fixture(scope="module")
def sberos():
    spec = importlib.util.spec_from_file_location("sberos_check", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _report():
    return {"sections": {}}


def test_folder_pairs_include_subfolders(sberos, tmp_path):
    root = make_profile(tmp_path / "p", {"INBOX": default_inbox_messages()[:1]})
    sub = root / "INBOX.sbd"
    write_mbox(sub / "Проекты", [make_plain_email("П", "тело", msg_id="p@t")])
    (sub / "Проекты.msf").write_bytes(b"")

    names = sorted(name for name, _size in sberos.list_folder_pairs(str(root)))

    assert names == sorted(list_email_folders(root)) == ["INBOX", "INBOX.sbd/Проекты"]


@pytest.mark.parametrize("active", ["aaaa.default", "zzzz.default-release"])
def test_profile_choice_matches_daemon(sberos, tmp_path, active):
    tb = tmp_path / ".thunderbird"
    for name in ("aaaa.default", "zzzz.default-release"):
        make_profile(tb / name, {"INBOX": default_inbox_messages()[:1]})
    (tb / "profiles.ini").write_text(
        "[Install4F96D1932A9F858E]\nDefault=%s\nLocked=1\n" % active
    )

    chosen = sberos.check_profile(_report(), str(tb))

    assert chosen == str(find_email_root(tmp_path))
    assert Path(chosen).parent.parent.name == active
