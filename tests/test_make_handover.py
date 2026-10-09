"""scripts/make_handover.sh: файлы с токеном и служебное не уходят в архив для
тестировщика, а живой токен в любом файле останавливает сборку архива.

Скрипт гоняется целиком на маленьком поддельном проекте. В PATH — только нужные
ему утилиты и заглушка date (уникальное имя архива и каталога в /tmp); uv в PATH
нет, поэтому шаг с тестами пропускается и /tmp/handover_tests.log не трогается.
"""

import os
import shutil
import subprocess
import uuid
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "make_handover.sh"
TOOLS = ("dirname", "rm", "mkdir", "grep", "sed", "tail", "rsync", "zip", "mv")
TOOLS += ("cut", "sort", "cat")

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("bash", *TOOLS)),
    reason="нужны bash, rsync, zip и coreutils",
)

# Токен и URL собираются в рантайме: литерал в исходнике остановил бы сборку
# архива из настоящего репозитория.
TOKEN = "Ab3" + "x9Kq2" * 7
LIVE_URL = "https://token:" + TOKEN + "@sberosc.sigma.sbrf.ru/repo/pypi/simple"
PLACEHOLDER_URL = (
    "https://token:" + "OSC_TOKEN" + "@sberosc.sigma.sbrf.ru/repo/pypi/simple"
)

PYPROJECT = """[project]
name = "fake"
dependencies = [
    "aiofiles>=25.1.0",
]

[tool.uv]
required-environments = ["sys_platform == 'linux' and platform_machine == 'x86_64'"]
"""


def _project(tmp_path, lock_text):
    proj = tmp_path / "proj"
    (proj / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, proj / "scripts" / "make_handover.sh")
    shutil.copy(ROOT / "Makefile", proj / "Makefile")  # там плейсхолдер $(OSC_TOKEN)
    (proj / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    (proj / "uv.lock").write_text(lock_text, encoding="utf-8")
    launcher = proj / "mcp_launcher.sh"
    launcher.write_text(
        "#!/bin/sh\ncat >/dev/null\necho local_search\n", encoding="utf-8"
    )
    launcher.chmod(0o755)
    (proj / "src").mkdir()
    (proj / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    return proj


def _run(tmp_path, proj):
    suffix = "pytest" + uuid.uuid4().hex[:12]
    tools = tmp_path / "tools"
    tools.mkdir()
    for tool in TOOLS:
        os.symlink(shutil.which(tool), tools / tool)
    date = tools / "date"
    date.write_text(f"#!/bin/sh\necho {suffix}\n", encoding="utf-8")
    date.chmod(0o755)
    try:
        out = subprocess.run(
            [shutil.which("bash"), str(proj / "scripts" / "make_handover.sh")],
            env=dict(os.environ, PATH=str(tools)),
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        shutil.rmtree(f"/tmp/lib-local-rag-mcp-{suffix}", ignore_errors=True)
        Path(f"/tmp/lib-local-rag-mcp-{suffix}.zip").unlink(missing_ok=True)
    return out, tmp_path / f"lib-local-rag-mcp-{suffix}.zip"


def test_archive_skips_token_files_and_service_dirs(tmp_path):
    proj = _project(tmp_path, f'source = {{ registry = "{PLACEHOLDER_URL}" }}\n')
    (proj / "Dockerfile").write_text(
        f'RUN echo "url = \\"{LIVE_URL}\\"" >> uv.toml\n', encoding="utf-8"
    )
    (proj / "pip.conf").write_text(
        f"[global]\nindex-url={LIVE_URL}\n", encoding="utf-8"
    )
    (proj / ".planning").mkdir()
    (proj / ".planning" / "review.md").write_text("ревью\n", encoding="utf-8")
    (proj / "CHANGES.diff").write_text("diff\n", encoding="utf-8")
    (proj / "notebooks").mkdir()
    (proj / "notebooks" / "mail.ipynb").write_text("{}\n", encoding="utf-8")

    out, archive = _run(tmp_path, proj)

    assert out.returncode == 0, out.stdout + out.stderr
    names = set(zipfile.ZipFile(archive).namelist())
    assert {
        "lib-local-rag/uv.lock",
        "lib-local-rag/Makefile",
        "lib-local-rag/src/app.py",
    } <= names
    top = {n.split("/")[1] for n in names if n.count("/") >= 1 and n.split("/")[1]}
    assert top.isdisjoint(
        {"Dockerfile", "pip.conf", ".planning", "CHANGES.diff", "notebooks"}
    )
    assert TOKEN not in out.stdout + out.stderr


def test_live_token_in_any_file_stops_archive(tmp_path):
    proj = _project(tmp_path, "version = 1\n")
    (proj / "docs").mkdir()
    (proj / "docs" / "notes.md").write_text(f"индекс: {LIVE_URL}\n", encoding="utf-8")

    out, archive = _run(tmp_path, proj)

    assert out.returncode == 1
    assert not archive.exists()
    assert "docs/notes.md" in out.stdout
    assert TOKEN not in out.stdout + out.stderr


def test_placeholder_in_lock_does_not_hide_live_token(tmp_path):
    lock = (
        f'source = {{ registry = "{PLACEHOLDER_URL}" }}\n'
        f'sdist = {{ url = "{LIVE_URL}" }}\n'
    )
    proj = _project(tmp_path, lock)

    out, archive = _run(tmp_path, proj)

    assert out.returncode == 1
    assert not archive.exists()
    assert "РЕАЛЬНЫЙ токен" in out.stdout
    assert TOKEN not in out.stdout + out.stderr
