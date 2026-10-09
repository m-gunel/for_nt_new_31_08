"""Песочница чтения: мост и демон пускают ровно то, до чего доходит обход.

Мост (mcp_server.py) — stdlib и src/ импортировать не может, поэтому правило в
нём продублировано; здесь проверяется, что копия не разошлась с
src/indexer/exclude.py. И граница списка папок: _validate_folders.
"""

import re

import pytest
from fastapi import HTTPException

import mcp_server
from src.indexer import scan_dir
from src.indexer.exclude import exclude_pattern, read_refusal
from src.indexer.scan_dir import iter_file_metadata
from src.server.router import _forbidden_folder, _validate_folders


def test_bridge_copy_of_exclude_pattern_is_exact():
    """Новое имя в exclude_pattern без правки моста — и мост отдаёт то, что
    индексатор пропускает. VERBOSE-шаблон сравнивается без пробелов и комментариев."""
    compact = re.sub(r"#[^\n]*|\s+", "", exclude_pattern.pattern)
    assert mcp_server._EXCLUDED_NAME.pattern == compact
    assert mcp_server._EXCLUDED_NAME.flags & re.IGNORECASE


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """Корень как поставочный /home: документы, мусор, скрытое и ссылки наружу."""
    monkeypatch.setattr(scan_dir, "network_mount_points", lambda: ())
    projects = tmp_path / "data" / "projects"
    projects.mkdir(parents=True)
    (projects / "a.txt").write_text("проект", encoding="utf-8")
    root = tmp_path / "home"
    files = [
        "u/Документы/отчёт.txt",
        "u/.секрет.txt",
        "u/.config/gh/hosts.txt",
        "u/.local/share/Trash/files/удалённый.txt",
        "u/node_modules/x/readme.txt",
        "u/snap/app/doc.txt",
        "u/build/out.txt",
    ]
    for rel in files:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("текст", encoding="utf-8")
    home = root / "u"
    (home / "Документы" / "Проекты").symlink_to(projects, target_is_directory=True)
    (home / "alias.txt").symlink_to(projects / "a.txt")
    (home / ".скрытая-ссылка").symlink_to(projects, target_is_directory=True)
    candidates = [str(root / rel) for rel in files] + [
        str(home / "Документы" / "Проекты" / "a.txt"),
        str(home / "alias.txt"),
        str(home / ".скрытая-ссылка" / "a.txt"),
        str(projects / "a.txt"),
    ]
    return root, candidates


@pytest.mark.parametrize("follow", [True, False])
def test_read_rule_matches_the_scan(tree, follow):
    """Читается ровно то, что отдаёт обход с тем же follow_symlinks."""
    root, candidates = tree
    scanned = {
        m.path
        for m in iter_file_metadata(
            str(root), check_parsers=False, follow_symlinks=follow
        )
    }
    for path in candidates:
        verdict = read_refusal(path, [str(root)], follow_symlinks=follow)
        assert (verdict is None) == (path in scanned), (path, verdict)


def test_bridge_agrees_with_daemon(tree):
    """Мост не знает follow_symlinks (GET /config его не отдаёт) и сверяет как
    при поставочном умолчании true."""
    root, candidates = tree
    for path in candidates:
        assert mcp_server._sandbox_refusal(path, [str(root)]) == read_refusal(
            path, [str(root)]
        ), path


@pytest.mark.parametrize(
    "folder",
    [
        "/",
        "/etc",
        "/etc/ssh",
        "/usr/share/doc",
        "/proc/self",
        "/sys",
        "/dev",
        "/var/log",
        "/home/u/.ssh",
        "/home/u/.config/gh",
        "/home/u/Документы/../.docker",
    ],
)
def test_dangerous_folders_are_forbidden(folder):
    assert _forbidden_folder(folder)


@pytest.mark.parametrize(
    "folder",
    [
        "/home",
        "/home/u/Документы",
        "/home/work/ivanov@sigma.sbrf.ru/проекты",
        "/home/sigma.sbrf.ru@ivanov",
        "/mnt/share",
        "/media/u/флешка",
        "/run/user/1000/gvfs/smb-share:server=fs,share=docs",
        "/opt/data",
        "/etcetera",
        "/usr2",
        "/variable",
    ],
)
def test_user_folders_are_allowed(folder):
    """Дом, оба вида путей SberOS, смонтированные и сетевые папки — штатные корни."""
    assert not _forbidden_folder(folder)


def test_symlink_to_fs_root_is_forbidden(tmp_path):
    link = tmp_path / "корень"
    link.symlink_to("/", target_is_directory=True)
    assert _forbidden_folder(str(link))


@pytest.mark.parametrize("name", ["/", "/etc", ".ssh"])
def test_validate_folders_refuses_dangerous_roots(tmp_path, name):
    """Список целиком отклоняется (400), конфиг не меняется — как у несуществующей папки."""
    folder = name if name.startswith("/") else str(tmp_path / name)
    if not name.startswith("/"):
        (tmp_path / name).mkdir()
    with pytest.raises(HTTPException) as caught:
        _validate_folders([str(tmp_path), folder])
    assert caught.value.status_code == 400
    assert folder in caught.value.detail


def test_validate_folders_keeps_user_folders(tmp_path):
    docs = tmp_path / "Документы"
    docs.mkdir()
    _validate_folders([str(docs)])
