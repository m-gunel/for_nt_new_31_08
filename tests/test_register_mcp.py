"""Регистрация MCP-моста в конфигах агентов.

Запись лежит в `mcp.<имя>`, а не в `mcp.servers.<имя>`: ключи внутри `mcp` —
сами имена серверов, и сервер "servers" без type и command агент счёл бы
невалидным и отбросил бы весь конфиг.

На SberOS pw_dir и $HOME терминала — разные каталоги, писать надо в оба.
Проверяются обход домов, отказ от записи под root, --doctor и токены агентов.

Скрипт лежит в scripts/ и пакетом не является, поэтому грузится по пути.
"""

import fcntl
import importlib.util
import json
import os
import stat
import threading
from pathlib import Path

import pytest
from pytest import fixture

_SPEC = importlib.util.spec_from_file_location(
    "register_mcp", Path(__file__).resolve().parents[1] / "scripts" / "register_mcp.py"
)
reg = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(reg)


class _Args:
    """Заменитель argparse.Namespace с теми же полями и умолчаниями, что у main()."""

    def __init__(self, **kw):
        self.name = reg.DEFAULT_NAME
        self.allow_write = False
        self.remove = False
        self.dry_run = False
        self.doctor = False
        self.list = False
        self.homes = None
        self.strict = False
        self.targets = None
        self.config = None
        self.schema = "opencode"
        self.agent = None
        self.rotate = False
        self.revoke = None
        self.__dict__.update(kw)


@fixture(autouse=True)
def _isolate_homes(tmp_path, monkeypatch):
    """Ни один тест не должен дотянуться до настоящих ~/.wsc и ~/.gigacode.

    LQRAG_REGISTER_HOMES заменяет обнаружение домов по passwd, поэтому main()
    без --home пишет только во временный каталог. HOME — туда же: от него
    считается легаси-цель gigatool. XDG_CONFIG_HOME разработчика тоже гасим.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(reg.HOMES_ENV, str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setenv(reg.UNIT_DIR_ENV, str(tmp_path / "no-units"))
    # pytest в Dockerfile идёт от root, а от root скрипт не пишет (root_refusal).
    # Подменяем только euid, по которому он решает; uid и права ФС — настоящие.
    if os.geteuid() == 0:
        monkeypatch.setattr(os, "geteuid", lambda: 1000)


@fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    return tmp_path


@fixture
def launcher(tmp_path):
    path = tmp_path / "mcp_launcher.sh"
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


@fixture
def sberos(tmp_path, monkeypatch):
    """Раскладка SberOS: pw_dir /home/DOMAIN@LOGIN, терминальный $HOME в /home/work.

    Корни подменены на временный каталог, passwd — на фиктивную запись, так
    что обнаружение домов работает по-настоящему, но вне реальной ФС.
    """
    monkeypatch.delenv(reg.HOMES_ENV, raising=False)
    root = tmp_path / "home"
    pw_dir = root / "dom.ru@ivanov"
    work = root / "work" / "ivanov@dom.ru"
    pw_dir.mkdir(parents=True)
    work.mkdir(parents=True)
    monkeypatch.setattr(reg, "HOME_ROOT", str(root))
    monkeypatch.setattr(reg, "WORK_HOME_ROOT", str(root / "work"))
    monkeypatch.setattr(reg, "_passwd_entry", lambda: ("ivanov@dom.ru", str(pw_dir)))
    monkeypatch.setenv("HOME", str(work))

    class Layout:
        pass

    layout = Layout()
    layout.pw_dir = pw_dir
    layout.work = work
    return layout


def _run(target_key, home, launcher, **kw):
    targets = dict(reg.TARGETS)
    targets.update(reg.CLEANUP_TARGETS)
    cleanup = target_key in reg.CLEANUP_TARGETS
    return reg.process_target(
        target_key,
        targets[target_key],
        launcher,
        _Args(**kw),
        cleanup_only=cleanup,
        home=None if cleanup else str(home),
        # Как main(): имя штатной цели — это и имя агента в ярлыке LQRAG_AGENT.
        agent=None if cleanup else target_key,
    )


def _main(launcher, *argv):
    return reg.main(["--launcher", launcher] + list(argv))


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _as_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)


def _as_real_root(monkeypatch):
    """Настоящий root целиком: и euid, и uid, и запись passwd.

    С одним geteuid()=0 обнаружение домов шло бы от пользователя, и тест
    «доказывал» бы запись, которой у настоящего root не бывает.
    """
    _as_root(monkeypatch)
    monkeypatch.setattr(os, "getuid", lambda: 0)
    monkeypatch.setattr(reg, "_passwd_entry", lambda: ("root", "/root"))


def _tree(path):
    """Снимок дерева: относительный путь -> содержимое файла (или None для каталога)."""
    result = {}
    for dirpath, dirnames, filenames in os.walk(str(path)):
        for name in dirnames:
            result[os.path.relpath(os.path.join(dirpath, name), str(path))] = None
        for name in filenames:
            full = os.path.join(dirpath, name)
            result[os.path.relpath(full, str(path))] = Path(full).read_bytes()
    return result


def test_opencode_entry_lies_directly_under_mcp(home, launcher):
    """Ключ — mcp.<имя>. Никакого промежуточного "servers"."""
    (home / ".wsc" / "config").mkdir(parents=True)
    _run("wsc", home, launcher)

    data = _read(home / ".wsc" / "config" / "wsc.json")
    assert "localrag" in data["mcp"]
    assert "servers" not in data["mcp"]
    entry = data["mcp"]["localrag"]
    assert entry["type"] == "local"
    assert entry["command"] == [launcher]
    assert entry["enabled"] is True


def test_opencode_entry_uses_environment_not_env(home, launcher):
    """Поле называется environment: env не проходит валидацию схемы."""
    (home / ".wsc" / "config").mkdir(parents=True)
    _run("wsc", home, launcher)

    entry = _read(home / ".wsc" / "config" / "wsc.json")["mcp"]["localrag"]
    assert "environment" in entry
    assert "env" not in entry


def test_opencode_entry_sets_explicit_timeout(home, launcher):
    """Умолчание агента — 5000 мс, а прогрев модели идёт 40-45 секунд."""
    (home / ".wsc" / "config").mkdir(parents=True)
    _run("wsc", home, launcher)

    entry = _read(home / ".wsc" / "config" / "wsc.json")["mcp"]["localrag"]
    assert entry["timeout"] >= 60000
    # Внутренний таймаут моста обязан быть НЕ БОЛЬШЕ внешнего, иначе агент
    # оборвёт запрос раньше, чем мост успеет вернуть осмысленную ошибку.
    assert float(entry["environment"]["LQRAG_TIMEOUT"]) * 1000 <= entry["timeout"]


def test_creates_config_when_agent_dir_exists(home, launcher):
    """wsc.json Workspace Computer сам не создаёт — создаём мы."""
    (home / ".wsc" / "config").mkdir(parents=True)
    assert not (home / ".wsc" / "config" / "wsc.json").exists()

    _run("wsc", home, launcher)

    assert (home / ".wsc" / "config" / "wsc.json").is_file()


def test_skips_and_creates_nothing_when_agent_absent(home, launcher):
    """Нет каталога агента — не трогаем домашний каталог вообще."""
    _run("gigacode", home, launcher)

    assert not (home / ".gigacode").exists()


def test_preserves_foreign_content(home, launcher):
    """Чужие настройки в конфиге агента переживают регистрацию."""
    cfg_dir = home / ".wsc" / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "wsc.json").write_text(
        json.dumps({"$schema": "./config-schema.json", "permission": {"bash": "ask"}}),
        encoding="utf-8",
    )

    _run("wsc", home, launcher)

    data = _read(cfg_dir / "wsc.json")
    assert data["permission"] == {"bash": "ask"}
    assert data["$schema"] == "./config-schema.json"
    assert "localrag" in data["mcp"]


def test_idempotent_second_run_does_not_rewrite(home, launcher):
    """Без этого .path-юнит и сервис зациклились бы друг на друге."""
    cfg_dir = home / ".wsc" / "config"
    cfg_dir.mkdir(parents=True)
    _run("wsc", home, launcher)
    before = (cfg_dir / "wsc.json").stat().st_mtime_ns

    _run("wsc", home, launcher)

    assert (cfg_dir / "wsc.json").stat().st_mtime_ns == before


def test_keeps_existing_file_mode(home, launcher):
    """Права чужого конфига не понижаются."""
    cfg_dir = home / ".wsc" / "config"
    cfg_dir.mkdir(parents=True)
    target = cfg_dir / "wsc.json"
    target.write_text("{}", encoding="utf-8")
    target.chmod(0o644)

    _run("wsc", home, launcher)

    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_reads_jsonc_with_comments_and_trailing_commas(home, launcher):
    """Агент допускает JSONC (allowComments, allowTrailingCommas)."""
    cfg_dir = home / ".wsc" / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "wsc.json").write_text(
        '// комментарий\n{\n  "permission": { "bash": "ask", },\n}\n', encoding="utf-8"
    )

    _run("wsc", home, launcher)

    data = _read(cfg_dir / "wsc.json")
    assert data["permission"] == {"bash": "ask"}
    assert "localrag" in data["mcp"]


def test_reads_config_with_bom(home, launcher):
    """BOM оставляют редакторы Windows; json.loads на нём падает, а файл валиден."""
    cfg_dir = home / ".wsc" / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "wsc.json").write_bytes(
        b"\xef\xbb\xbf" + json.dumps({"permission": {"bash": "ask"}}).encode("utf-8")
    )

    _run("wsc", home, launcher)

    raw = (cfg_dir / "wsc.json").read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    data = json.loads(raw.decode("utf-8"))
    assert data["permission"] == {"bash": "ask"}
    assert "localrag" in data["mcp"]


def test_restores_entry_after_agent_overwrote_config(home, launcher):
    """Сценарий СУС: приложение поставили позже и оно затёрло запись."""
    cfg_dir = home / ".wsc" / "config"
    cfg_dir.mkdir(parents=True)
    _run("wsc", home, launcher)
    (cfg_dir / "wsc.json").write_text(json.dumps({"permission": {}}), encoding="utf-8")

    _run("wsc", home, launcher)

    assert "localrag" in _read(cfg_dir / "wsc.json")["mcp"]


def test_remove_drops_entry_but_keeps_the_rest(home, launcher):
    cfg_dir = home / ".wsc" / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "wsc.json").write_text(
        json.dumps({"permission": {"bash": "ask"}}), encoding="utf-8"
    )
    _run("wsc", home, launcher)

    _run("wsc", home, launcher, remove=True)

    data = _read(cfg_dir / "wsc.json")
    assert data["permission"] == {"bash": "ask"}
    assert "mcp" not in data


def test_migration_removes_broken_servers_key(home, launcher):
    """Невалидный mcp.servers от прежней схемы снимается."""
    cfg_dir = home / ".wsc" / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "wsc.json").write_text(
        json.dumps(
            {
                "mcp": {
                    "servers": {"localrag": {"type": "local", "command": ["/old.sh"]}}
                }
            }
        ),
        encoding="utf-8",
    )

    _run("wsc", home, launcher)

    mcp = _read(cfg_dir / "wsc.json")["mcp"]
    assert "servers" not in mcp
    assert mcp["localrag"]["command"] == [launcher]


def test_migration_spares_a_real_server_named_servers(home, launcher):
    """Если кто-то и правда назвал свой сервер «servers» — это чужая запись."""
    cfg_dir = home / ".wsc" / "config"
    cfg_dir.mkdir(parents=True)
    real = {"type": "local", "command": ["/chuzhoy.sh"], "enabled": True}
    (cfg_dir / "wsc.json").write_text(
        json.dumps({"mcp": {"servers": real}}), encoding="utf-8"
    )

    _run("wsc", home, launcher)

    assert _read(cfg_dir / "wsc.json")["mcp"]["servers"] == real


def test_legacy_target_is_cleaned_but_not_written_to(home, launcher):
    """В легаси-каталог новых записей не делаем, мусор — убираем."""
    legacy = home / ".config" / "gigatool"
    legacy.mkdir(parents=True)
    (legacy / "opencode.json").write_text(
        json.dumps(
            {
                "model": "giga/x",
                "mcp": {"servers": {"localrag": {"command": ["/old.sh"]}}},
            }
        ),
        encoding="utf-8",
    )

    _run("gigatool", home, launcher)

    data = _read(legacy / "opencode.json")
    assert data["model"] == "giga/x"
    assert "mcp" not in data


def test_gigacode_uses_flat_mcp_servers_with_string_command(home, launcher):
    """У GigaCode схема ДРУГАЯ: command строкой, args отдельно, env — и всё."""
    (home / ".gigacode").mkdir()
    (home / ".gigacode" / "settings.json").write_text(
        json.dumps({"ide": {"hasSeenNudge": True}, "$version": 3, "mcpServers": {}}),
        encoding="utf-8",
    )

    _run("gigacode", home, launcher)

    data = _read(home / ".gigacode" / "settings.json")
    assert data["ide"] == {"hasSeenNudge": True}
    assert data["$version"] == 3
    entry = data["mcpServers"]["localrag"]
    # Поля транспорта нет: в рабочем конфиге GigaCode запись состоит из command.
    assert set(entry) == {"command", "args", "env"}
    assert entry["command"] == launcher
    assert isinstance(entry["command"], str)  # не массив, как в opencode-схеме
    assert entry["args"] == []
    assert entry["env"]["LQRAG_TIMEOUT"] == reg.BRIDGE_TIMEOUT_S
    assert "mcp" not in data


def test_gigacode_rewrites_old_entry_with_type_stdio(home, launcher):
    """Запись прежней версии с type: "stdio" перезаписывается без поля type."""
    (home / ".gigacode").mkdir()
    old = {
        "type": "stdio",
        "command": launcher,
        "args": [],
        "env": {"LQRAG_TIMEOUT": reg.BRIDGE_TIMEOUT_S},
    }
    settings = home / ".gigacode" / "settings.json"
    settings.write_text(json.dumps({"mcpServers": {"localrag": old}}), encoding="utf-8")

    _run("gigacode", home, launcher)

    entry = _read(settings)["mcpServers"]["localrag"]
    assert "type" not in entry
    assert entry["command"] == launcher


def _opt_out_wsc_entry(launcher, **override):
    """Запись WSC закрытой установки: выключена в агенте, write-инструменты скрыты."""
    entry = {
        "type": "local",
        "command": [launcher],
        "enabled": False,
        "timeout": reg.MCP_TIMEOUT_MS,
        "environment": {
            "LQRAG_TIMEOUT": reg.BRIDGE_TIMEOUT_S,
            "LQRAG_ALLOW_WRITE": "0",
            reg.AGENT_ENV: "wsc",
        },
    }
    entry.update(override)
    return entry


def test_wsc_opt_out_is_not_rewritten(home, launcher):
    """enabled: false и LQRAG_ALLOW_WRITE=0 — выбор пользователя: файл не трогаем."""
    cfg = home / ".wsc" / "config" / "wsc.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        json.dumps({"mcp": {"localrag": _opt_out_wsc_entry(launcher)}}),
        encoding="utf-8",
    )
    before = cfg.read_bytes()

    assert _run("wsc", home, launcher) == reg.UNCHANGED

    assert cfg.read_bytes() == before


def test_stale_wsc_entry_is_updated_but_keeps_opt_out(home, launcher):
    """Свои поля (command, timeout, LQRAG_TIMEOUT) обновляем, поля пользователя сохраняем."""
    cfg = home / ".wsc" / "config" / "wsc.json"
    cfg.parent.mkdir(parents=True)
    old = _opt_out_wsc_entry(
        "/old/mcp_launcher.sh",
        timeout=5000,
        environment={
            "LQRAG_TIMEOUT": "30",
            "LQRAG_ALLOW_WRITE": "0",
            "LQRAG_AUTOSTART": "0",
        },
    )
    cfg.write_text(json.dumps({"mcp": {"localrag": old}}), encoding="utf-8")

    assert _run("wsc", home, launcher) == reg.WRITTEN

    entry = _read(cfg)["mcp"]["localrag"]
    assert entry["command"] == [launcher]
    assert entry["timeout"] == reg.MCP_TIMEOUT_MS
    assert entry["enabled"] is False
    assert entry["environment"] == {
        "LQRAG_TIMEOUT": reg.BRIDGE_TIMEOUT_S,
        "LQRAG_ALLOW_WRITE": "0",
        "LQRAG_AUTOSTART": "0",
        reg.AGENT_ENV: "wsc",
    }


def test_wsc_entry_keys_outside_schema_are_still_dropped(home, launcher):
    """Сохраняются только enabled и environment: лишний ключ отверг бы весь конфиг."""
    cfg = home / ".wsc" / "config" / "wsc.json"
    cfg.parent.mkdir(parents=True)
    entry = _opt_out_wsc_entry(launcher, env={"LQRAG_TIMEOUT": "30"})
    cfg.write_text(json.dumps({"mcp": {"localrag": entry}}), encoding="utf-8")

    assert _run("wsc", home, launcher) == reg.WRITTEN

    entry = _read(cfg)["mcp"]["localrag"]
    assert "env" not in entry
    assert entry["enabled"] is False
    assert entry["environment"]["LQRAG_ALLOW_WRITE"] == "0"


def test_gigacode_entry_keeps_opt_out_in_env(home, launcher):
    """У GigaCode opt-out — в env: command и type прежней версии чинятся, env пользователя остаётся."""
    settings = home / ".gigacode" / "settings.json"
    settings.parent.mkdir()
    env = {
        "LQRAG_TIMEOUT": reg.BRIDGE_TIMEOUT_S,
        "LQRAG_ALLOW_WRITE": "0",
        reg.AGENT_ENV: "gigacode",
    }
    old = {
        "type": "stdio",
        "command": "/old/mcp_launcher.sh",
        "args": [],
        "env": dict(env),
    }
    settings.write_text(json.dumps({"mcpServers": {"localrag": old}}), encoding="utf-8")

    assert _run("gigacode", home, launcher) == reg.WRITTEN

    assert _read(settings)["mcpServers"]["localrag"] == {
        "command": launcher,
        "args": [],
        "env": env,
    }
    assert _run("gigacode", home, launcher) == reg.UNCHANGED


def test_unit_run_keeps_opt_out_in_both_agents(home, launcher, capsys):
    """Ровно запуск юнита (--all): opt-out в обоих агентах переживает прогон."""
    wsc = home / ".wsc" / "config" / "wsc.json"
    wsc.parent.mkdir(parents=True)
    wsc.write_text(
        json.dumps({"mcp": {"localrag": _opt_out_wsc_entry(launcher)}}),
        encoding="utf-8",
    )
    giga = home / ".gigacode" / "settings.json"
    giga.parent.mkdir()
    env = {
        "LQRAG_TIMEOUT": reg.BRIDGE_TIMEOUT_S,
        "LQRAG_ALLOW_WRITE": "0",
        reg.AGENT_ENV: "gigacode",
    }
    giga.write_text(
        json.dumps(
            {"mcpServers": {"localrag": {"command": launcher, "args": [], "env": env}}}
        ),
        encoding="utf-8",
    )
    before = [wsc.read_bytes(), giga.read_bytes()]

    assert _main(launcher, "--all") == 0

    assert [wsc.read_bytes(), giga.read_bytes()] == before
    assert capsys.readouterr().out.count("без изменений") == 2


def test_allow_write_flag_overrides_opt_out(home, launcher):
    """Явный --allow-write сильнее записанного LQRAG_ALLOW_WRITE=0; enabled не трогает."""
    cfg = home / ".wsc" / "config" / "wsc.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        json.dumps({"mcp": {"localrag": _opt_out_wsc_entry(launcher)}}),
        encoding="utf-8",
    )

    assert _run("wsc", home, launcher, allow_write=True) == reg.WRITTEN

    entry = _read(cfg)["mcp"]["localrag"]
    assert entry["environment"]["LQRAG_ALLOW_WRITE"] == "1"
    assert entry["enabled"] is False


def test_broken_config_in_one_target_does_not_stop_another(home, launcher):
    """Юнит дёргается автоматически: один битый файл не должен ронять прогон."""
    (home / ".wsc" / "config").mkdir(parents=True)
    legacy = home / ".config" / "gigatool"
    legacy.mkdir(parents=True)
    (legacy / "opencode.json").write_text('{ "mcp": { broken', encoding="utf-8")

    _run("gigatool", home, launcher)  # не выбрасывает
    _run("wsc", home, launcher)

    assert "localrag" in _read(home / ".wsc" / "config" / "wsc.json")["mcp"]


def test_backup_is_made_once_before_first_edit(home, launcher):
    cfg_dir = home / ".wsc" / "config"
    cfg_dir.mkdir(parents=True)
    original = json.dumps({"permission": {"bash": "ask"}})
    (cfg_dir / "wsc.json").write_text(original, encoding="utf-8")

    _run("wsc", home, launcher)

    backup = cfg_dir / "wsc.json.localrag-backup"
    assert backup.read_text(encoding="utf-8") == original


def test_every_rewrite_of_commented_file_is_backed_up(home, launcher, capsys):
    """Комментарии JSON не сохраняет: копия — при каждой перезаписи файла с ними, а не только первой."""
    cfg_dir = home / ".wsc" / "config"
    cfg_dir.mkdir(parents=True)
    cfg = cfg_dir / "wsc.json"
    first = "// первый\n{}\n"
    cfg.write_text(first, encoding="utf-8")
    assert _run("wsc", home, launcher) == reg.WRITTEN
    later = []
    for comment in ("// второй", "// третий"):
        # Агент переписал конфиг без нашей записи и с новыми комментариями.
        text = '%s\n{"permission": {"bash": "ask"}}\n' % comment
        cfg.write_text(text, encoding="utf-8")
        later.append(text)
        assert _run("wsc", home, launcher) == reg.WRITTEN

    assert (cfg_dir / "wsc.json.localrag-backup").read_text(encoding="utf-8") == first
    copies = sorted(cfg_dir.glob("wsc.json.localrag-backup-*"))
    assert sorted(p.read_text(encoding="utf-8") for p in copies) == sorted(later)
    assert "комментарии при записи не сохраняются" in capsys.readouterr().err


def test_rewrite_without_comments_adds_no_copies(home, launcher):
    """Без комментариев терять нечего: копии рядом с конфигом не копятся."""
    cfg_dir = home / ".wsc" / "config"
    cfg_dir.mkdir(parents=True)
    cfg = cfg_dir / "wsc.json"
    cfg.write_text(json.dumps({"permission": {}}), encoding="utf-8")
    _run("wsc", home, launcher)
    cfg.write_text(json.dumps({"permission": {"bash": "ask"}}), encoding="utf-8")

    assert _run("wsc", home, launcher) == reg.WRITTEN

    assert sorted(p.name for p in cfg_dir.iterdir()) == [
        "wsc.json",
        "wsc.json.localrag-backup",
    ]


def test_dry_run_changes_nothing(home, launcher):
    cfg_dir = home / ".wsc" / "config"
    cfg_dir.mkdir(parents=True)

    _run("wsc", home, launcher, dry_run=True)

    assert not (cfg_dir / "wsc.json").exists()


def test_discover_homes_puts_pw_dir_first_and_adds_sberos_pair(sberos):
    """Терминал на SberOS: $HOME в /home/work, а конфиг WSC — в pw_dir."""
    assert reg.discover_homes() == [str(sberos.pw_dir), str(sberos.work)]


def test_discover_homes_finds_work_dir_by_name_when_home_is_pw_dir(sberos, monkeypatch):
    """Юнит и postinst идут с HOME=pw_dir — /home/work находится по имени."""
    monkeypatch.setenv("HOME", str(sberos.pw_dir))

    assert reg.discover_homes() == [str(sberos.pw_dir), str(sberos.work)]


def test_discover_homes_dedups_symlinked_home(sberos, monkeypatch, tmp_path):
    """$HOME-симлинк на pw_dir — тот же каталог, второй раз не обходится."""
    link = tmp_path / "link-to-pw"
    link.symlink_to(sberos.pw_dir)
    monkeypatch.setenv("HOME", str(link))

    homes = reg.discover_homes()

    assert homes.count(str(sberos.pw_dir)) == 1
    assert str(link) not in homes


def test_discover_homes_skips_missing_candidates(sberos):
    sberos.work.rmdir()

    assert reg.discover_homes() == [str(sberos.pw_dir)]


def test_sberos_pair_for_name_without_at(sberos):
    """/home/work/<имя> — для любого имени; «@» нужен только для /home/DOMAIN@LOGIN.

    .path-юниты следят за /home/work/%u безусловно, а без «@» бывают и
    локальная учётка, и SSSD с use_fully_qualified_names=false.
    """
    work_root = reg.WORK_HOME_ROOT
    assert reg._sberos_pair("ivanov") == [os.path.join(work_root, "ivanov")]
    assert reg._sberos_pair("ivanov@dom.ru") == [
        os.path.join(work_root, "ivanov@dom.ru"),
        os.path.join(reg.HOME_ROOT, "dom.ru@ivanov"),
    ]
    # Кривое «@» не мешает первой половине, но вторую не строит.
    assert reg._sberos_pair("a@b@c") == [os.path.join(work_root, "a@b@c")]
    assert reg._sberos_pair("@dom.ru") == [os.path.join(work_root, "@dom.ru")]
    for bad in ("", None, ".", "..", "a/b", "../x"):
        assert reg._sberos_pair(bad) == []


def test_discover_homes_finds_work_dir_for_name_without_at(tmp_path, monkeypatch):
    """Юнит с HOME=pw_dir у пользователя без «@» видит и /home/work/<имя>."""
    monkeypatch.delenv(reg.HOMES_ENV, raising=False)
    root = tmp_path / "home"
    pw_dir = root / "ivanov"
    work = root / "work" / "ivanov"
    pw_dir.mkdir(parents=True)
    work.mkdir(parents=True)
    monkeypatch.setattr(reg, "HOME_ROOT", str(root))
    monkeypatch.setattr(reg, "WORK_HOME_ROOT", str(root / "work"))
    monkeypatch.setattr(reg, "_passwd_entry", lambda: ("ivanov", str(pw_dir)))
    monkeypatch.setenv("HOME", str(pw_dir))

    assert reg.discover_homes() == [str(pw_dir), str(work)]


def test_check_home_rejects_foreign_and_missing(tmp_path):
    """Писать можно только в свой каталог: $HOME бывает унаследован от root."""
    st, reason = reg.check_home(str(tmp_path), os.getuid() + 1)
    assert st is None and "владелец" in reason

    st, reason = reg.check_home(str(tmp_path / "nope"), os.getuid())
    assert st is None and reason == "нет каталога"

    st, reason = reg.check_home(str(tmp_path), os.getuid())
    assert st is not None and reason is None


def test_explicit_homes_replace_discovery(sberos, monkeypatch, tmp_path):
    """LQRAG_REGISTER_HOMES — список через os.pathsep, пустые элементы игнорируются."""
    a, b = tmp_path / "a", tmp_path / "b"
    monkeypatch.setenv(reg.HOMES_ENV, os.pathsep.join([str(a), "", str(b), str(a)]))

    assert reg.discover_homes() == [str(a), str(b)]
    # --home сильнее переменной.
    assert reg.discover_homes([str(b)]) == [str(b)]


def test_main_writes_to_every_home_where_agent_is_installed(
    tmp_path, launcher, monkeypatch, capsys
):
    h1, h2 = tmp_path / "h1", tmp_path / "h2"
    (h1 / ".wsc").mkdir(parents=True)
    (h1 / ".gigacode").mkdir()
    (h2 / ".gigacode").mkdir(parents=True)
    monkeypatch.setenv(reg.HOMES_ENV, os.pathsep.join([str(h1), str(h2)]))

    assert _main(launcher) == 0

    out = capsys.readouterr().out
    assert "localrag" in _read(h1 / ".wsc" / "config" / "wsc.json")["mcp"]
    assert "localrag" in _read(h1 / ".gigacode" / "settings.json")["mcpServers"]
    assert "localrag" in _read(h2 / ".gigacode" / "settings.json")["mcpServers"]
    assert not (h2 / ".wsc").exists()
    # Эти строки показывает postinst — формат держим.
    assert "wsc       записано: %s" % (h1 / ".wsc" / "config" / "wsc.json") in out
    assert "wsc       пропуск: Workspace Computer не установлен в %s" % h2 in out


def test_home_option_replaces_env_homes(tmp_path, launcher, monkeypatch):
    h1, h2 = tmp_path / "h1", tmp_path / "h2"
    (h1 / ".wsc").mkdir(parents=True)
    (h2 / ".wsc").mkdir(parents=True)
    monkeypatch.setenv(reg.HOMES_ENV, str(h1))

    assert _main(launcher, "--home", str(h2)) == 0

    assert (h2 / ".wsc" / "config" / "wsc.json").is_file()
    assert not (h1 / ".wsc" / "config").exists()


def test_same_file_seen_from_two_homes_is_processed_once(
    tmp_path, launcher, monkeypatch, capsys
):
    """.wsc в /home/work — симлинк в pw_dir: один файл, одна строка вывода."""
    h1, h2 = tmp_path / "h1", tmp_path / "h2"
    (h1 / ".wsc").mkdir(parents=True)
    h2.mkdir()
    (h2 / ".wsc").symlink_to(h1 / ".wsc")
    monkeypatch.setenv(reg.HOMES_ENV, os.pathsep.join([str(h1), str(h2)]))

    _main(launcher, "--target", "wsc")

    out = capsys.readouterr().out
    assert len([line for line in out.splitlines() if line.startswith("wsc ")]) == 1


def test_remove_walks_all_homes(tmp_path, launcher, monkeypatch):
    h1, h2 = tmp_path / "h1", tmp_path / "h2"
    (h1 / ".wsc").mkdir(parents=True)
    (h2 / ".gigacode").mkdir(parents=True)
    monkeypatch.setenv(reg.HOMES_ENV, os.pathsep.join([str(h1), str(h2)]))
    _main(launcher)

    assert _main(launcher, "--remove") == 0

    assert "mcp" not in _read(h1 / ".wsc" / "config" / "wsc.json")
    assert "localrag" not in _read(h2 / ".gigacode" / "settings.json")["mcpServers"]


def test_main_second_run_changes_nothing(home, launcher, capsys):
    """Идемпотентность через main(): «без изменений», файл не переписан."""
    (home / ".wsc").mkdir()
    (home / ".gigacode").mkdir()
    _main(launcher)
    files = [
        home / ".wsc" / "config" / "wsc.json",
        home / ".gigacode" / "settings.json",
    ]
    before = [(f.stat().st_mtime_ns, f.read_bytes()) for f in files]
    capsys.readouterr()

    assert _main(launcher) == 0

    out = capsys.readouterr().out
    assert "записано" not in out
    assert out.count("без изменений") == 2
    assert [(f.stat().st_mtime_ns, f.read_bytes()) for f in files] == before


def test_no_homes_found_is_reported_but_not_a_failure(
    tmp_path, launcher, monkeypatch, capsys
):
    """Юнит дёргается .path-срабатываниями: failed заглушил бы следующие."""
    monkeypatch.delenv(reg.HOMES_ENV)
    monkeypatch.setattr(reg, "_passwd_entry", lambda: (None, None))
    monkeypatch.setenv("HOME", str(tmp_path / "nope"))
    monkeypatch.setattr(os, "geteuid", lambda: 1000)

    assert _main(launcher) == 0

    assert "не найден ни один домашний каталог" in capsys.readouterr().err


def test_list_shows_targets_for_each_home(home, launcher, capsys):
    assert reg.main(["--list"]) == 0

    assert str(home / ".wsc" / "config" / "wsc.json") in capsys.readouterr().out


def test_process_target_returns_status(home, launcher):
    """По статусу main() решает про код --strict и про фразу о перезапуске."""
    assert _run("gigacode", home, launcher) == reg.SKIPPED
    (home / ".wsc" / "config").mkdir(parents=True)
    assert _run("wsc", home, launcher, dry_run=True) == reg.PLANNED
    assert _run("wsc", home, launcher) == reg.WRITTEN
    assert _run("wsc", home, launcher) == reg.UNCHANGED
    (home / ".wsc" / "config" / "wsc.json").write_text("{ broken", encoding="utf-8")
    assert _run("wsc", home, launcher) == reg.FAILED


def test_strict_zero_when_agent_found(home, launcher):
    (home / ".wsc").mkdir()

    assert _main(launcher, "--strict") == 0  # записано
    assert _main(launcher, "--strict") == 0  # без изменений — тоже успех


def test_strict_three_when_no_agent_anywhere(home, launcher):
    """Ни одного агента ни в одном доме — не ошибка, но и не «выполнено»."""
    assert _main(launcher, "--strict") == 3
    assert _main(launcher) == 0  # без --strict — 0


def test_strict_one_when_a_target_fails(home, launcher, capsys):
    """Одна ОШИБКА — 1, даже если другая цель записана."""
    (home / ".wsc" / "config").mkdir(parents=True)
    (home / ".wsc" / "config" / "wsc.json").write_text(
        '{ "mcp": { broken', encoding="utf-8"
    )
    (home / ".gigacode").mkdir()

    assert _main(launcher, "--strict") == 1

    captured = capsys.readouterr()
    assert "ОШИБКА" in captured.err
    assert "localrag" in _read(home / ".gigacode" / "settings.json")["mcpServers"]
    # Без --strict — 0: юнит не должен упираться в StartLimit.
    assert _main(launcher) == 0


def test_strict_one_when_legacy_cleanup_fails(home, launcher):
    (home / ".wsc").mkdir()
    legacy = home / ".config" / "gigatool"
    legacy.mkdir(parents=True)
    (legacy / "opencode.json").write_text('{ "mcp": { broken', encoding="utf-8")

    assert _main(launcher, "--strict") == 1


def test_non_utf8_config_fails_only_its_target(home, launcher, capsys):
    """Конфиг не в UTF-8 — ОШИБКА своей цели; следующая цель регистрируется, прогон не падает."""
    settings = home / ".gigacode" / "settings.json"
    settings.parent.mkdir()
    raw = '{"ide": {}, "note": "закрытая установка"}'.encode("cp1251")
    settings.write_bytes(raw)
    (home / ".wsc").mkdir()

    assert _main(launcher) == 0

    err = capsys.readouterr().err
    assert "ОШИБКА" in err and "UTF-8" in err
    assert settings.read_bytes() == raw
    assert "localrag" in _read(home / ".wsc" / "config" / "wsc.json")["mcp"]
    assert _main(launcher, "--strict") == 1


def test_unexpected_error_in_one_target_does_not_stop_others(home, launcher, capsys):
    """Непредвиденный сбой цели — её ОШИБКА, а не обрыв прогона.

    Одиночный суррогат в JSON читается, но обратно в UTF-8 не кодируется.
    """
    settings = home / ".gigacode" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"note": "\\ud800"}', encoding="utf-8")
    (home / ".wsc").mkdir()

    assert _main(launcher) == 0

    assert "ОШИБКА" in capsys.readouterr().err
    assert "localrag" in _read(home / ".wsc" / "config" / "wsc.json")["mcp"]
    assert _main(launcher, "--strict") == 1


def test_strict_one_when_no_homes_found(tmp_path, launcher, monkeypatch):
    monkeypatch.delenv(reg.HOMES_ENV)
    monkeypatch.setattr(reg, "_passwd_entry", lambda: (None, None))
    monkeypatch.setenv("HOME", str(tmp_path / "nope"))

    assert _main(launcher, "--strict") == 1


def test_strict_with_config(tmp_path, home, launcher):
    cfg = tmp_path / "agent" / "cfg.json"
    cfg.parent.mkdir()
    agent = ("--agent", "custom")

    assert _main(launcher, "--strict", "--config", str(cfg), *agent) == 0
    cfg.write_text("[1, 2", encoding="utf-8")
    assert _main(launcher, "--strict", "--config", str(cfg), *agent) == 1
    nope = str(tmp_path / "nope" / "cfg.json")
    assert _main(launcher, "--strict", "--config", nope, *agent) == 3
    assert not (tmp_path / "nope").exists()


def test_config_without_agent_is_refused_before_writing(tmp_path, home, launcher):
    """Запись без ярлыка LQRAG_AGENT мосту бесполезна: токена нет — 401 всегда."""
    cfg = tmp_path / "agent" / "cfg.json"
    cfg.parent.mkdir()

    with pytest.raises(SystemExit) as exc:
        _main(launcher, "--config", str(cfg))

    assert exc.value.code == 2
    assert not cfg.exists()
    # --remove ярлык не нужен: файла нет — «агент не найден», а не отказ.
    assert _main(launcher, "--strict", "--config", str(cfg), "--remove") == 3


def test_restart_hint_only_when_something_written(home, launcher, capsys):
    """«Перезапустите агента» — только если конфиг агента реально переписан."""
    hint = "Перезапустите агента"

    _main(launcher)  # агентов нет — всё «пропуск»
    assert hint not in capsys.readouterr().out

    (home / ".wsc").mkdir()
    _main(launcher, "--dry-run")
    assert hint not in capsys.readouterr().out

    _main(launcher)
    assert hint in capsys.readouterr().out

    _main(launcher)  # второй прогон — «без изменений»
    assert hint not in capsys.readouterr().out

    (home / ".wsc" / "config" / "wsc.json").write_text("{ broken", encoding="utf-8")
    _main(launcher)  # ОШИБКА
    assert hint not in capsys.readouterr().out


def test_restart_hint_not_for_legacy_cleanup_only(home, launcher, capsys):
    """Вычистить мусор из легаси-каталога — не повод перезапускать агента."""
    legacy = home / ".config" / "gigatool"
    legacy.mkdir(parents=True)
    (legacy / "opencode.json").write_text(
        json.dumps({"mcp": {"servers": {"localrag": {"command": ["/old.sh"]}}}}),
        encoding="utf-8",
    )

    _main(launcher)

    out = capsys.readouterr().out
    assert "вычищен невалидный mcp.servers" in out
    assert "Перезапустите агента" not in out


def _victim(tmp_path, name="victim.txt", mode=0o600):
    """Файл ВНЕ дома, на который указывают подложенные ссылки."""
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    victim = outside / name
    victim.write_text("не трогать", encoding="utf-8")
    victim.chmod(mode)
    return victim


def test_symlink_in_place_of_tmp_is_not_followed(home, launcher, tmp_path):
    """Симлинк на месте временного файла прежних версий: цель ссылки не тронута."""
    cfg_dir = home / ".wsc" / "config"
    cfg_dir.mkdir(parents=True)
    cfg = cfg_dir / "wsc.json"
    cfg.write_text("{}", encoding="utf-8")
    cfg.chmod(0o644)
    victim = _victim(tmp_path)
    (cfg_dir / "wsc.json.localrag.tmp").symlink_to(victim)

    assert _run("wsc", home, launcher) == reg.WRITTEN

    assert victim.read_text(encoding="utf-8") == "не трогать"
    assert stat.S_IMODE(victim.stat().st_mode) == 0o600
    assert not cfg.is_symlink()
    assert "localrag" in _read(cfg)["mcp"]
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o644


def test_symlink_in_place_of_backup_is_not_followed(home, launcher, tmp_path):
    """Симлинк на месте копии: ни на существующий файл, ни висячий — запись не идёт."""
    cfg_dir = home / ".wsc" / "config"
    cfg_dir.mkdir(parents=True)
    cfg = cfg_dir / "wsc.json"
    cfg.write_text(json.dumps({"permission": {}}), encoding="utf-8")
    victim = _victim(tmp_path)
    backup = cfg_dir / "wsc.json.localrag-backup"
    backup.symlink_to(victim)

    _run("wsc", home, launcher)

    assert victim.read_text(encoding="utf-8") == "не трогать"
    assert stat.S_IMODE(victim.stat().st_mode) == 0o600
    assert "localrag" in _read(cfg)["mcp"]

    # Висячая ссылка: файл по ней не создаётся (exists() сказал бы «нет»).
    dangling_target = tmp_path / "outside" / "created-by-root"
    backup.unlink()
    backup.symlink_to(dangling_target)
    cfg.write_text(json.dumps({"permission": {"bash": "ask"}}), encoding="utf-8")

    _run("wsc", home, launcher)

    assert not os.path.lexists(str(dangling_target))
    assert backup.is_symlink()


def test_stale_tmp_from_interrupted_run_does_not_block(home, launcher):
    """Остаток прерванного прогона не мешает записи, а новых временных файлов нет."""
    cfg_dir = home / ".wsc" / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "wsc.json.localrag.tmp").write_text("{ half", encoding="utf-8")

    assert _run("wsc", home, launcher) == reg.WRITTEN

    assert "localrag" in _read(cfg_dir / "wsc.json")["mcp"]
    assert sorted(p.name for p in cfg_dir.iterdir()) == [
        "wsc.json",
        "wsc.json.localrag.tmp",
    ]


def test_parallel_runs_do_not_share_temp_file(home, launcher, monkeypatch):
    """Второй прогон между созданием временного файла и replace первого не ломает его."""
    cfg_dir = home / ".wsc" / "config"
    cfg_dir.mkdir(parents=True)
    cfg = str(cfg_dir / "wsc.json")
    real_replace = os.replace
    inner = []

    def replace_after_second_run(src, dst):
        if dst == cfg and not inner:
            inner.append(True)
            reg.save_config(cfg, {"by": "B"})
        real_replace(src, dst)

    monkeypatch.setattr(reg.os, "replace", replace_after_second_run)

    reg.save_config(cfg, {"by": "A"})

    assert _read(cfg) == {"by": "A"}
    assert os.listdir(str(cfg_dir)) == ["wsc.json"]


def test_mode_is_set_by_descriptor_despite_umask(home, launcher):
    """Права ставятся fchmod'ом: umask 077 не понижает 0644 существующего конфига."""
    cfg_dir = home / ".wsc" / "config"
    cfg_dir.mkdir(parents=True)
    cfg = cfg_dir / "wsc.json"
    cfg.write_text(json.dumps({"permission": {}}), encoding="utf-8")
    cfg.chmod(0o644)
    old = os.umask(0o077)
    try:
        _run("wsc", home, launcher)
    finally:
        os.umask(old)

    assert stat.S_IMODE(cfg.stat().st_mode) == 0o644
    backup = cfg_dir / "wsc.json.localrag-backup"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o644


def test_new_config_is_created_0600(home, launcher):
    cfg_dir = home / ".wsc" / "config"
    cfg_dir.mkdir(parents=True)
    old = os.umask(0)
    try:
        _run("wsc", home, launcher)
    finally:
        os.umask(old)

    assert stat.S_IMODE((cfg_dir / "wsc.json").stat().st_mode) == 0o600


def test_root_refuses_to_write_without_explicit_homes(
    sberos, launcher, monkeypatch, capsys
):
    """sudo python3 register_mcp.py не должен оставить конфиги с владельцем root."""
    (sberos.pw_dir / ".wsc").mkdir()
    _as_root(monkeypatch)
    monkeypatch.setenv("SUDO_USER", "ivanov@dom.ru")

    assert _main(launcher) == 2

    err = capsys.readouterr().err
    assert "собственностью root" in err
    assert "runuser -u ivanov@dom.ru -- python3" in err
    assert not (sberos.pw_dir / ".wsc" / "config").exists()


def test_root_refuses_remove_too(sberos, launcher, monkeypatch):
    _as_root(monkeypatch)

    assert _main(launcher, "--remove") == 2


def test_root_may_read(sberos, launcher, monkeypatch):
    """--doctor, --list и --dry-run ничего не пишут — им запрет не нужен."""
    (sberos.pw_dir / ".wsc").mkdir()
    _as_root(monkeypatch)

    assert _main(launcher, "--doctor") == 0
    assert _main(launcher, "--list") == 0
    assert _main(launcher, "--dry-run") == 0
    assert not (sberos.pw_dir / ".wsc" / "config").exists()


def test_real_root_may_dry_run_explicit_home(sberos, launcher, monkeypatch, capsys):
    """--dry-run по явному дому от root разрешён и ничего не пишет."""
    (sberos.pw_dir / ".wsc").mkdir()
    before = _tree(sberos.pw_dir)
    _as_real_root(monkeypatch)

    assert _main(launcher, "--dry-run", "--home", str(sberos.pw_dir)) == 0

    assert "получилось бы" in capsys.readouterr().out
    assert _tree(sberos.pw_dir) == before


@pytest.mark.parametrize(
    "mode",
    [
        "auto",
        "home",
        "env-homes",
        "config",
        "remove",
        "target",
        "strict",
        "allow-write",
        "rotate",
        "revoke",
    ],
)
def test_root_refuses_every_writing_mode(sberos, launcher, monkeypatch, capsys, mode):
    """От root не пишем НИЧЕГО — ни по найденным домам, ни по явным, ни в --config.

    Иначе root ходил бы по симлинкам, подложенным в пользовательский каталог.
    """
    for home in (sberos.pw_dir, sberos.work):
        (home / ".wsc" / "config").mkdir(parents=True)
        (home / ".wsc" / "config" / "wsc.json").write_text(
            json.dumps(
                {"mcp": {"localrag": {"type": "local", "command": ["/old.sh"]}}}
            ),
            encoding="utf-8",
        )
        (home / ".gigacode").mkdir()
    before = _tree(sberos.pw_dir.parent)
    os.chmod(launcher, 0o644)  # без +x: отказ обязан случиться и до chmod лаунчера
    _as_real_root(monkeypatch)
    argv = {
        "auto": [],
        "home": ["--home", str(sberos.pw_dir)],
        "env-homes": [],
        "config": [
            "--config",
            str(sberos.pw_dir / ".wsc" / "config" / "wsc.json"),
            "--agent",
            "custom",
        ],
        "remove": ["--remove", "--home", str(sberos.pw_dir)],
        "target": ["--target", "wsc", "--home", str(sberos.work)],
        "strict": ["--strict", "--home", str(sberos.pw_dir)],
        "allow-write": ["--allow-write", "--home", str(sberos.pw_dir)],
        # Перевыпуск и отзыв токенов тоже пишут на диск.
        "rotate": ["--rotate", "--home", str(sberos.pw_dir)],
        "revoke": ["--revoke", "wsc", "--home", str(sberos.pw_dir)],
    }[mode]
    if mode == "env-homes":
        monkeypatch.setenv(
            reg.HOMES_ENV, os.pathsep.join([str(sberos.pw_dir), str(sberos.work)])
        )

    assert _main(launcher, *argv) == 2

    err = capsys.readouterr().err
    assert "запущен от root и ничего не записал" in err
    assert "runuser -u " in err
    assert "--allow-root" not in err
    assert _tree(sberos.pw_dir.parent) == before
    assert stat.S_IMODE(os.stat(launcher).st_mode) == 0o644


def test_allow_root_flag_is_gone(launcher, monkeypatch):
    """Флага --allow-root нет — argparse его не знает."""
    _as_real_root(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        _main(launcher, "--allow-root")

    assert exc.value.code == 2  # код argparse на неизвестный аргумент


@pytest.mark.skipif(os.geteuid() == 0, reason="временный каталог принадлежит root")
def test_root_refusal_names_owner_of_explicit_home(
    tmp_path, launcher, monkeypatch, capsys
):
    """Без sudo (su -, runuser) подсказка называет владельца явно заданного дома."""
    import pwd

    owner = pwd.getpwuid(os.stat(str(tmp_path)).st_uid).pw_name
    _as_real_root(monkeypatch)

    assert _main(launcher, "--home", str(tmp_path)) == 2

    assert "runuser -u %s -- python3" % owner in capsys.readouterr().err


def _doctor(launcher, capsys, *argv):
    capsys.readouterr()
    assert _main(launcher, "--doctor", *argv) == 0
    return capsys.readouterr().out


def _line(out, needle):
    return next(line for line in out.splitlines() if needle in line)


def test_doctor_shows_identity_and_homes(sberos, launcher, capsys):
    out = _doctor(launcher, capsys)

    assert "uid %d" % os.getuid() in out
    assert "pw_dir:       %s" % sberos.pw_dir in out
    assert "$HOME:        %s" % sberos.work in out
    # pw_dir совпадает со вторым элементом SberOS-пары — метки сливаются.
    assert "%s  [pw_dir, SberOS]" % sberos.pw_dir in out
    assert "== дом %s" % sberos.work in out


@pytest.mark.skipif(os.geteuid() == 0, reason="root читает файл с правами 000")
def test_doctor_reports_unreadable_unit(tmp_path, launcher, monkeypatch, capsys):
    """Юниты 0600 root: systemd --user отвечает «could not be found»."""
    units = tmp_path / "units"
    units.mkdir()
    broken = units / "lib-local-rag.service"
    broken.write_text("[Unit]\n", encoding="utf-8")
    broken.chmod(0)
    (units / "lib-local-rag-mcp-register.service").write_text(
        "[Unit]\n", encoding="utf-8"
    )
    monkeypatch.setenv(reg.UNIT_DIR_ENV, str(units))
    try:
        out = _doctor(launcher, capsys)
    finally:
        broken.chmod(0o644)

    line = _line(out, "lib-local-rag.service ")
    assert "НЕ ЧИТАЕТСЯ пользователем" in line
    assert '"could not be found"' in line
    assert "читается" in _line(out, "lib-local-rag-mcp-register.service")
    assert "нет файла" in _line(out, "lib-local-rag-mcp-register-wsc.path")


@pytest.mark.skipif(os.geteuid() == 0, reason="root проходит os.access на любом файле")
def test_doctor_reports_launcher_without_exec_and_does_not_fix_it(launcher, capsys):
    os.chmod(launcher, 0o644)

    out = _doctor(launcher, capsys)

    assert "НЕ ДОСТУПЕН на чтение и запуск" in out
    assert stat.S_IMODE(os.stat(launcher).st_mode) == 0o644


def test_doctor_entry_states(home, launcher, capsys):
    (home / ".wsc" / "config").mkdir(parents=True)
    (home / ".wsc" / "config" / "wsc.json").write_text("{}", encoding="utf-8")

    assert "запись:    нет" in _doctor(launcher, capsys)
    _main(launcher)
    assert "запись:    есть" in _doctor(launcher, capsys)


def test_doctor_reports_stale_entry_duplicate_and_hides_env(home, launcher, capsys):
    cfg_dir = home / ".wsc" / "config"
    cfg_dir.mkdir(parents=True)
    config = {
        "mcp": {
            "localrag": {
                "type": "local",
                "command": ["/old/checkout/mcp_launcher.sh"],
                "enabled": True,
                "timeout": 5000,
                "environment": {"LQRAG_TIMEOUT": "30"},
            },
            "old-rag": {
                "type": "local",
                "command": ["/opt/old/mcp_launcher.sh"],
                "environment": {"TOKEN": "s3cr3t-value"},
            },
            "other": {"type": "local", "command": ["/usr/bin/other-mcp"]},
        }
    }
    (cfg_dir / "wsc.json").write_text(json.dumps(config), encoding="utf-8")
    before = (cfg_dir / "wsc.json").read_bytes()

    out = _doctor(launcher, capsys)

    state = _line(out, "запись:")
    assert "УСТАРЕЛА" in state and "command" in state and "timeout" in state
    duplicates = [line for line in out.splitlines() if "дубль" in line]
    assert len(duplicates) == 1 and "old-rag" in duplicates[0]
    assert "s3cr3t-value" not in out
    assert (cfg_dir / "wsc.json").read_bytes() == before


def test_doctor_accepts_opt_out(home, launcher, capsys):
    """Opt-out — допустимая запись, а не «УСТАРЕЛА»: --doctor не подталкивает его откатить."""
    cfg_dir = home / ".wsc" / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "wsc.json").write_text(
        json.dumps({"mcp": {"localrag": _opt_out_wsc_entry(launcher)}}),
        encoding="utf-8",
    )
    (home / ".gigacode").mkdir()
    env = {
        "LQRAG_TIMEOUT": reg.BRIDGE_TIMEOUT_S,
        "LQRAG_ALLOW_WRITE": "0",
        reg.AGENT_ENV: "gigacode",
    }
    (home / ".gigacode" / "settings.json").write_text(
        json.dumps(
            {"mcpServers": {"localrag": {"command": launcher, "args": [], "env": env}}}
        ),
        encoding="utf-8",
    )

    out = _doctor(launcher, capsys)

    states = [line for line in out.splitlines() if "запись:" in line]
    assert len(states) == 2
    assert not any("УСТАРЕЛА" in line for line in states)
    assert "выключена в агенте" in _line(out, "запись:    есть, но")


def test_doctor_finds_duplicate_in_gigacode_args(home, launcher, capsys):
    (home / ".gigacode").mkdir()
    (home / ".gigacode" / "settings.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "rag-sh": {"command": "sh", "args": ["/x/mcp_launcher.sh"]}
                }
            }
        ),
        encoding="utf-8",
    )

    out = _doctor(launcher, capsys)

    assert "«rag-sh»" in _line(out, "дубль")


def test_doctor_reports_non_utf8_config(home, launcher, capsys):
    """--doctor не падает на конфиге не в UTF-8, а называет причину."""
    (home / ".gigacode").mkdir()
    (home / ".gigacode" / "settings.json").write_bytes(
        '{"note": "привет"}'.encode("cp1251")
    )

    line = _line(_doctor(launcher, capsys), "НЕ ЧИТАЕТСЯ")

    assert "UTF-8" in line


# Токены агентов: секрет — ~/.files-search/tokens/<агент>.token для моста,
# {агент: sha256} — ~/.files-search/auth.json для демона. В конфиг агента идёт
# только ярлык LQRAG_AGENT: конфиг можно показать в тикете, файлы токенов — нет.


def _tokens(home):
    """{агент: содержимое файла токена} в этом доме."""
    tokens = home / reg.FILES_SEARCH_DIR / reg.TOKENS_DIR_NAME
    if not tokens.is_dir():
        return {}
    return {
        path.name[: -len(reg.TOKEN_SUFFIX)]: path.read_text(encoding="utf-8").strip()
        for path in tokens.iterdir()
        if path.name.endswith(reg.TOKEN_SUFFIX)
    }


def _auth(home):
    return _read(home / reg.FILES_SEARCH_DIR / reg.AUTH_FILE_NAME)


def _token_file(home, agent="wsc"):
    return (
        home / reg.FILES_SEARCH_DIR / reg.TOKENS_DIR_NAME / (agent + reg.TOKEN_SUFFIX)
    )


def _mode(path):
    return stat.S_IMODE(os.lstat(str(path)).st_mode)


def _plant_token(home, content, mode=0o600, agent="wsc"):
    """Файл токена, оставленный до регистрации."""
    path = _token_file(home, agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(mode)
    return path


# Валидный по виду токен, который регистрация не должна взять себе.
_PLANTED = "planted-token-known-to-attacker-0123456789"


def test_registration_mints_per_agent_tokens(home, launcher, capsys):
    """Файл токена 0600 + хэш в auth.json + несекретный ярлык в конфиге."""
    (home / ".wsc").mkdir()
    (home / ".gigacode").mkdir()

    _main(launcher)

    tokens = _tokens(home)
    assert set(tokens) == {"wsc", "gigacode"}
    assert tokens["wsc"] != tokens["gigacode"]
    auth = _auth(home)
    assert auth == {name: reg.sha256_hex(token) for name, token in tokens.items()}
    assert _mode(_token_file(home)) == 0o600
    entry = _read(home / ".wsc" / "config" / "wsc.json")["mcp"]["localrag"]
    assert entry["environment"][reg.AGENT_ENV] == "wsc"
    assert tokens["wsc"] not in json.dumps(entry)
    out = capsys.readouterr()
    assert tokens["wsc"] not in out.out and tokens["wsc"] not in out.err


def test_second_registration_reuses_existing_tokens(home, launcher):
    """Перевыпуск на каждом срабатывании .path ломал бы все мосты."""
    (home / ".wsc").mkdir()
    (home / ".gigacode").mkdir()
    _main(launcher)
    auth_file = home / reg.FILES_SEARCH_DIR / reg.AUTH_FILE_NAME
    before = (_tokens(home), _auth(home), auth_file.stat().st_mtime_ns)

    _main(launcher)

    assert (_tokens(home), _auth(home), auth_file.stat().st_mtime_ns) == before


def test_tokens_are_shared_across_homes(tmp_path, launcher, monkeypatch):
    """Разные токены по домам — мост предъявил бы демону неизвестный ему токен."""
    h1, h2 = tmp_path / "h1", tmp_path / "h2"
    (h1 / ".wsc").mkdir(parents=True)
    (h2 / ".wsc").mkdir(parents=True)
    monkeypatch.setenv(reg.HOMES_ENV, os.pathsep.join([str(h1), str(h2)]))

    _main(launcher)

    assert _tokens(h1)["wsc"] == _tokens(h2)["wsc"]
    assert _auth(h1)["wsc"] == _auth(h2)["wsc"]


def test_rotate_keeps_one_token_across_homes(tmp_path, launcher, monkeypatch):
    """--rotate на SberOS (два дома): один новый токен на агента в обоих домах."""
    h1, h2 = tmp_path / "h1", tmp_path / "h2"
    (h1 / ".wsc").mkdir(parents=True)
    h2.mkdir()
    monkeypatch.setenv(reg.HOMES_ENV, os.pathsep.join([str(h1), str(h2)]))
    _main(launcher)
    old = _tokens(h1)["wsc"]

    assert _main(launcher, "--rotate") == 0

    new = _tokens(h1)["wsc"]
    assert new != old
    assert _tokens(h2)["wsc"] == new
    assert _auth(h1)["wsc"] == _auth(h2)["wsc"] == reg.sha256_hex(new)


def test_token_from_second_home_is_reused(tmp_path, launcher, monkeypatch):
    """Пропал токен в pw_dir (RESET=1) — берётся годный из второго дома, а не новый."""
    h1, h2 = tmp_path / "h1", tmp_path / "h2"
    (h1 / ".wsc").mkdir(parents=True)
    h2.mkdir()
    monkeypatch.setenv(reg.HOMES_ENV, os.pathsep.join([str(h1), str(h2)]))
    _main(launcher)
    token = _tokens(h2)["wsc"]
    _token_file(h1).unlink()

    _main(launcher)

    assert _tokens(h1)["wsc"] == token
    assert _auth(h1)["wsc"] == reg.sha256_hex(token)


def _two_homes(tmp_path, monkeypatch):
    """Два дома, как на SberOS; WSC установлен в первом."""
    h1, h2 = tmp_path / "h1", tmp_path / "h2"
    (h1 / ".wsc").mkdir(parents=True)
    h2.mkdir()
    monkeypatch.setenv(reg.HOMES_ENV, os.pathsep.join([str(h1), str(h2)]))
    return h1, h2


def test_revoke_in_one_home_is_not_undone_by_copy_in_other(
    tmp_path, launcher, monkeypatch
):
    """Отзыв дошёл не во все дома (--home, сбой) — регистрация не вернёт старый токен."""
    h1, h2 = _two_homes(tmp_path, monkeypatch)
    _main(launcher)
    old = _tokens(h1)["wsc"]

    assert _main(launcher, "--revoke", "wsc", "--home", str(h1)) == 0
    _main(launcher)

    new = _tokens(h1)["wsc"]
    assert new != old
    assert _tokens(h2)["wsc"] == new
    assert _auth(h1)["wsc"] == _auth(h2)["wsc"] == reg.sha256_hex(new)


def test_partial_rotate_is_not_rolled_back(tmp_path, launcher, monkeypatch, capsys):
    """--rotate не дописал один дом — следующая регистрация не возвращает старый токен."""
    h1, h2 = _two_homes(tmp_path, monkeypatch)
    _main(launcher)
    old = _tokens(h1)["wsc"]
    real_write = reg._write_private
    # Сбой в первом доме (pw_dir): прежде регистрация брала токен оттуда.
    h1_token = str(_token_file(h1))

    def fail_in_h1(path, payload):
        if path == h1_token:
            raise OSError(5, "Input/output error")
        real_write(path, payload)

    monkeypatch.setattr(reg, "_write_private", fail_in_h1)
    assert _main(launcher, "--rotate") == 1  # и без --strict: ручной режим
    assert "ОШИБКА" in capsys.readouterr().err
    assert _tokens(h1)["wsc"] == old
    monkeypatch.setattr(reg, "_write_private", real_write)

    _main(launcher)

    assert old not in (_tokens(h1)["wsc"], _tokens(h2)["wsc"])
    assert _tokens(h1)["wsc"] == _tokens(h2)["wsc"]
    assert reg.sha256_hex(old) not in (_auth(h1)["wsc"], _auth(h2)["wsc"])


def test_token_readable_by_others_in_one_home_is_reissued_everywhere(
    tmp_path, launcher, monkeypatch
):
    """Копия 0644 в одном доме — секрет мог утечь: новый токен во все дома."""
    h1, h2 = _two_homes(tmp_path, monkeypatch)
    _main(launcher)
    old = _tokens(h1)["wsc"]
    _token_file(h2).chmod(0o644)

    _main(launcher)

    assert _tokens(h1)["wsc"] != old
    assert _tokens(h1)["wsc"] == _tokens(h2)["wsc"]
    assert _mode(_token_file(h2)) == 0o600


def test_auth_json_writable_by_others_is_not_carried_over(home, launcher, capsys):
    """Демон такой файл отвергает: подложенный в него хэш в новый файл не попадает."""
    (home / ".wsc").mkdir()
    _main(launcher)
    auth_file = home / reg.FILES_SEARCH_DIR / reg.AUTH_FILE_NAME
    planted = dict(_auth(home), intruder=reg.sha256_hex(_PLANTED))
    auth_file.write_text(json.dumps(planted), encoding="utf-8")
    auth_file.chmod(0o666)
    capsys.readouterr()

    _main(launcher)

    assert set(_auth(home)) == {"wsc"}
    assert _mode(auth_file) == 0o600
    assert "отвергнут демоном" in capsys.readouterr().err


def test_auth_entries_daemon_would_skip_are_dropped(home, launcher):
    (home / ".wsc").mkdir()
    root = home / reg.FILES_SEARCH_DIR
    root.mkdir()
    entries = {
        "good": reg.sha256_hex("x"),
        "Bad Name": reg.sha256_hex("y"),
        "short": "abc",
    }
    (root / reg.AUTH_FILE_NAME).write_text(json.dumps(entries), encoding="utf-8")

    _main(launcher)

    assert set(_auth(home)) == {"good", "wsc"}


def test_forget_removes_token_traces_in_every_home(tmp_path, launcher, monkeypatch):
    """uninstall.sh удаляет один ~/.files-search, а токены лежат в каждом доме."""
    h1, h2 = _two_homes(tmp_path, monkeypatch)
    _main(launcher)
    _main(launcher, "--rotate")
    (h1 / reg.FILES_SEARCH_DIR / "lance").mkdir()  # данные демона — не наше дело

    assert _main(launcher, "--forget") == 0

    assert sorted(os.listdir(str(h1 / reg.FILES_SEARCH_DIR))) == ["lance"]
    assert not (h2 / reg.FILES_SEARCH_DIR).exists()
    assert _read(h1 / ".wsc" / "config" / "wsc.json")["mcp"]["localrag"]


def test_forget_dry_run_changes_nothing(tmp_path, launcher, monkeypatch, capsys):
    h1, h2 = _two_homes(tmp_path, monkeypatch)
    _main(launcher)
    before = (_tokens(h1), _tokens(h2))

    assert _main(launcher, "--forget", "--dry-run") == 0

    assert (_tokens(h1), _tokens(h2)) == before
    assert "(план)" in capsys.readouterr().out


def test_registration_preserves_foreign_auth_entries(home, launcher):
    """auth.json — общее хранилище: чужого агента никто не отзывал."""
    (home / ".wsc").mkdir()
    root = home / reg.FILES_SEARCH_DIR
    root.mkdir()
    foreign = {"megacode": reg.sha256_hex("чужой")}
    (root / reg.AUTH_FILE_NAME).write_text(json.dumps(foreign), encoding="utf-8")

    _main(launcher)

    assert _auth(home)["megacode"] == reg.sha256_hex("чужой")
    assert "megacode" not in _tokens(home)


def test_tokens_only_for_agents_with_config(home, launcher):
    """Неустановленному агенту токен не выдаётся."""
    (home / ".wsc").mkdir()

    _main(launcher)

    assert set(_tokens(home)) == {"wsc"}
    assert set(_auth(home)) == {"wsc"}


def test_revoked_agent_without_config_stays_revoked(home, launcher):
    """Агента удалили и отозвали — следующая регистрация токен ему не вернёт."""
    (home / ".wsc").mkdir()
    (home / ".gigacode").mkdir()
    _main(launcher)
    (home / ".gigacode" / "settings.json").unlink()
    (home / ".gigacode").rmdir()
    _main(launcher, "--revoke", "gigacode")

    _main(launcher)

    assert set(_tokens(home)) == {"wsc"}
    assert set(_auth(home)) == {"wsc"}


def test_rotate_replaces_every_token_and_digest(home, launcher, capsys):
    (home / ".wsc").mkdir()
    (home / ".gigacode").mkdir()
    _main(launcher)
    old = _tokens(home)

    assert _main(launcher, "--rotate") == 0

    new = _tokens(home)
    assert all(new[name] != old[name] for name in old)
    assert _auth(home) == {name: reg.sha256_hex(token) for name, token in new.items()}
    out = capsys.readouterr()
    assert new["wsc"] not in out.out and new["wsc"] not in out.err


def test_rotate_names_tokens_it_did_not_rotate(home, launcher, capsys):
    """Токен агента без конфига --rotate не трогает, но называет: он ещё действует."""
    (home / ".wsc").mkdir()
    (home / ".gigacode").mkdir()
    _main(launcher)
    (home / ".gigacode" / "settings.json").unlink()
    (home / ".gigacode").rmdir()
    old = _tokens(home)["gigacode"]
    capsys.readouterr()

    _main(launcher, "--rotate")

    assert _tokens(home)["gigacode"] == old
    assert "не разобран): gigacode —" in _line(
        capsys.readouterr().err, "не перевыпущены"
    )


def test_revoke_removes_token_and_digest_only_for_one_agent(home, launcher):
    (home / ".wsc").mkdir()
    (home / ".gigacode").mkdir()
    _main(launcher)

    assert _main(launcher, "--revoke", "wsc") == 0

    assert set(_tokens(home)) == {"gigacode"}
    assert set(_auth(home)) == {"gigacode"}
    # Регистрация не снимается: отзыв — смена секрета, а не удаление агента.
    assert _read(home / ".wsc" / "config" / "wsc.json")["mcp"]["localrag"]


def test_revoke_is_idempotent(home, launcher, capsys):
    (home / ".wsc").mkdir()
    _main(launcher)
    _main(launcher, "--revoke", "wsc")

    assert _main(launcher, "--revoke", "wsc") == 0

    assert "нет следов" in capsys.readouterr().out


def test_revoke_walks_all_homes(tmp_path, launcher, monkeypatch):
    h1, h2 = tmp_path / "h1", tmp_path / "h2"
    (h1 / ".wsc").mkdir(parents=True)
    h2.mkdir()
    monkeypatch.setenv(reg.HOMES_ENV, os.pathsep.join([str(h1), str(h2)]))
    _main(launcher)

    assert _main(launcher, "--revoke", "wsc") == 0

    assert _tokens(h1) == _tokens(h2) == {}
    assert _auth(h1) == _auth(h2) == {}


def test_revoke_failure_returns_one(home, launcher, capsys):
    """Отзыв не удался — код 1 и строка ОШИБКА, а не молчаливый успех."""
    (home / ".wsc").mkdir()
    _main(launcher)
    secret = _token_file(home)
    secret.unlink()
    secret.mkdir()
    (secret / "keep").write_text("", encoding="utf-8")

    assert _main(launcher, "--revoke", "wsc") == 1

    assert "ОШИБКА" in capsys.readouterr().err


def test_remove_leaves_tokens_alone(home, launcher):
    """Снятие регистрации — не отзыв: для этого --revoke, а purge сносит всё."""
    (home / ".wsc").mkdir()
    _main(launcher)
    before = (_tokens(home), _auth(home))

    _main(launcher, "--remove")

    assert (_tokens(home), _auth(home)) == before


def test_dry_run_mints_nothing(home, launcher, capsys):
    (home / ".wsc").mkdir()

    _main(launcher, "--dry-run")

    assert not (home / reg.FILES_SEARCH_DIR).exists()
    assert "(план) авторизация %s: wsc (выдан)" % home in capsys.readouterr().out


def test_dry_run_revoke_and_rotate_change_nothing(home, launcher):
    (home / ".wsc").mkdir()
    _main(launcher)
    before = _tree(home / reg.FILES_SEARCH_DIR)

    _main(launcher, "--rotate", "--dry-run")
    _main(launcher, "--revoke", "wsc", "--dry-run")

    assert _tree(home / reg.FILES_SEARCH_DIR) == before


def test_token_storage_is_private_and_leaves_no_temp_files(home, launcher):
    """Каталоги 0700, файлы 0600 при любом umask; после записи — ни одного временного файла."""
    (home / ".wsc").mkdir()
    old = os.umask(0)
    try:
        _main(launcher)
        _main(launcher, "--rotate")
    finally:
        os.umask(old)

    root = home / reg.FILES_SEARCH_DIR
    expected = [
        reg.AUTH_FILE_NAME,
        reg.LOCK_FILE_NAME,
        reg.TOKENS_DIR_NAME,
        reg.WITHDRAWN_FILE_NAME,  # от --rotate
    ]
    assert sorted(os.listdir(str(root))) == sorted(expected)
    assert os.listdir(str(root / reg.TOKENS_DIR_NAME)) == ["wsc" + reg.TOKEN_SUFFIX]
    assert _mode(root) == _mode(root / reg.TOKENS_DIR_NAME) == 0o700
    assert _mode(root / reg.AUTH_FILE_NAME) == _mode(_token_file(home)) == 0o600
    assert _mode(root / reg.WITHDRAWN_FILE_NAME) == 0o600


def test_shared_auth_json_is_rewritten_0600(home, launcher):
    (home / ".wsc").mkdir()
    _main(launcher)
    auth_file = home / reg.FILES_SEARCH_DIR / reg.AUTH_FILE_NAME
    auth_file.chmod(0o644)
    before = _auth(home)

    _main(launcher)

    assert _mode(auth_file) == 0o600
    assert _auth(home) == before


def test_registration_waits_for_lock_of_another_run(home, launcher):
    """Пока другой прогон держит .register.lock, токены не пишутся; после — пишутся."""
    (home / ".wsc").mkdir()
    root = home / reg.FILES_SEARCH_DIR
    root.mkdir(mode=0o700)
    worker = threading.Thread(target=_main, args=(launcher,))
    with open(str(root / reg.LOCK_FILE_NAME), "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        worker.start()
        worker.join(timeout=0.5)
        waited = worker.is_alive() and not _tokens(home)

    worker.join(timeout=30)

    assert waited
    assert not worker.is_alive()
    assert set(_tokens(home)) == {"wsc"}


def test_token_symlink_is_not_reused_or_followed(home, launcher, tmp_path):
    """Симлинк на месте файла токена: его содержимое не берётся, цель не переписывается."""
    (home / ".wsc").mkdir()
    victim = _victim(tmp_path, name="token-victim.txt")
    victim.write_text(_PLANTED, encoding="utf-8")
    secret = _token_file(home)
    secret.parent.mkdir(parents=True)
    secret.symlink_to(victim)

    _main(launcher)

    assert victim.read_text(encoding="utf-8") == _PLANTED
    assert not secret.is_symlink()
    token = _tokens(home)["wsc"]
    assert token != _PLANTED
    assert _auth(home)["wsc"] == reg.sha256_hex(token)


@pytest.mark.parametrize(
    "content, mode",
    [
        (_PLANTED.encode("ascii"), 0o644),  # доступен группе и всем
        (b"\xff\xfe not utf-8 \x00", 0o600),  # битый файл не роняет регистрацию
        (b"short", 0o600),  # не похоже на токен
    ],
)
def test_unfit_token_file_is_replaced(home, launcher, content, mode):
    (home / ".wsc").mkdir()
    secret = _plant_token(home, content, mode)

    assert _main(launcher, "--strict") == 0

    token = _tokens(home)["wsc"]
    assert token.encode("ascii") != content.strip()
    assert _mode(secret) == 0o600
    assert _auth(home)["wsc"] == reg.sha256_hex(token)


def test_foreign_token_file_is_not_reused(home, launcher, monkeypatch):
    """Файл токена чужого владельца (явный --home в чужом каталоге) не переиспользуется."""
    (home / ".wsc").mkdir()
    _plant_token(home, _PLANTED.encode("ascii"))
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)

    _main(launcher)

    assert _tokens(home)["wsc"] != _PLANTED


def test_failed_token_write_is_error_in_strict(home, launcher, capsys):
    """Токен не записан — мосту будет 401, и postinst не должен писать «выполнено»."""
    (home / ".wsc").mkdir()
    root = home / reg.FILES_SEARCH_DIR
    root.mkdir()
    (root / reg.TOKENS_DIR_NAME).write_text("не каталог", encoding="utf-8")

    assert _main(launcher, "--strict") == 1

    assert "ОШИБКА" in capsys.readouterr().err
    assert "localrag" in _read(home / ".wsc" / "config" / "wsc.json")["mcp"]
    assert _auth(home) == {}  # хэша без файла токена не бывает
    assert _main(launcher) == 0  # без --strict — 0, юнит не упирается в StartLimit


def test_failed_auth_write_is_error_in_strict(home, launcher, capsys):
    (home / ".wsc").mkdir()
    root = home / reg.FILES_SEARCH_DIR
    root.mkdir()
    (root / reg.AUTH_FILE_NAME).mkdir()

    assert _main(launcher, "--strict") == 1

    assert "ОШИБКА" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["--revoke", "../x"],
        ["--revoke", "WSC"],
        ["--revoke", "wsc\n"],
        ["--revoke", "a" * 33],
        ["--config", "cfg.json", "--agent", "../../x"],
        ["--agent", "custom"],  # --agent без --config
        ["--rotate", "--remove"],
        ["--revoke", "wsc", "--rotate"],
    ],
)
def test_bad_agent_names_and_mode_mixes_are_refused(launcher, capsys, argv):
    with pytest.raises(SystemExit) as exc:
        _main(launcher, *argv)

    assert exc.value.code == 2


def test_config_with_agent_gets_label_and_token(tmp_path, home, launcher):
    cfg = tmp_path / "agent" / "cfg.json"
    cfg.parent.mkdir()

    assert _main(launcher, "--strict", "--config", str(cfg), "--agent", "custom") == 0

    assert _read(cfg)["mcp"]["localrag"]["environment"][reg.AGENT_ENV] == "custom"
    assert _auth(home)["custom"] == reg.sha256_hex(_tokens(home)["custom"])


def test_config_without_agent_dir_gets_no_token(tmp_path, home, launcher):
    cfg = tmp_path / "nope" / "cfg.json"

    assert _main(launcher, "--strict", "--config", str(cfg), "--agent", "custom") == 3

    assert not (home / reg.FILES_SEARCH_DIR).exists()


def test_doctor_reports_auth_and_tokens_without_secret(home, launcher, capsys):
    (home / ".wsc").mkdir()
    (home / ".gigacode").mkdir()
    (home / ".gigacode" / "settings.json").write_text("{}", encoding="utf-8")

    before = _doctor(launcher, capsys)
    _main(launcher)
    after = _doctor(launcher, capsys)

    assert "нет файла — демон отвечает 401" in _line(before, "авторизация:")
    assert "НЕТ — будет выдан" in _line(before, "токен:")
    assert _line(after, "авторизация:").endswith(": gigacode, wsc")
    tokens = _tokens(home)
    assert [line for line in after.splitlines() if "токен:" in line] == [
        "  токен:     есть, хэш в auth.json совпадает"
    ] * 2
    assert all(token not in after for token in tokens.values())


def test_doctor_reports_digest_mismatch(home, launcher, capsys):
    (home / ".wsc").mkdir()
    _main(launcher)
    auth_file = home / reg.FILES_SEARCH_DIR / reg.AUTH_FILE_NAME
    auth_file.write_text(
        json.dumps({"wsc": reg.sha256_hex("другой")}), encoding="utf-8"
    )

    out = _doctor(launcher, capsys)

    assert "НЕ СОВПАДАЕТ" in _line(out, "токен:")


@pytest.mark.parametrize("mode", [0o620, 0o602], ids=["g+w", "o+w"])
def test_doctor_reports_auth_json_the_daemon_rejects(home, launcher, capsys, mode):
    """Права, при которых демон закрыт для всех, --doctor называет прямо."""
    (home / ".wsc").mkdir()
    _main(launcher)
    (home / reg.FILES_SEARCH_DIR / reg.AUTH_FILE_NAME).chmod(mode)

    line = _line(_doctor(launcher, capsys), reg.AUTH_FILE_NAME + ":")

    assert "ОТВЕРГНУТ" in line and "%04o" % mode in line


def test_doctor_marks_entry_without_agent_label_stale(home, launcher, capsys):
    """Запись без LQRAG_AGENT мост не свяжет с токеном — это УСТАРЕЛА."""
    cfg = home / ".wsc" / "config" / "wsc.json"
    cfg.parent.mkdir(parents=True)
    entry = _opt_out_wsc_entry(launcher, enabled=True)
    del entry["environment"][reg.AGENT_ENV]
    cfg.write_text(json.dumps({"mcp": {"localrag": entry}}), encoding="utf-8")

    state = _line(_doctor(launcher, capsys), "запись:")

    assert "УСТАРЕЛА" in state and "environment" in state


def test_token_names_match_bridge_and_daemon():
    """Имена и хэш повторены в мосте и демоне: stdlib-скрипт их не импортирует."""
    import mcp_server
    from src import config
    from src.server import auth

    assert (mcp_server.TOKENS_DIR_NAME, mcp_server.TOKEN_SUFFIX) == (
        reg.TOKENS_DIR_NAME,
        reg.TOKEN_SUFFIX,
    )
    assert mcp_server.PORT_FILE_RELATIVE.split(os.sep)[0] == reg.FILES_SEARCH_DIR
    assert (config.AUTH_FILE_NAME, config.TOKENS_DIR_NAME) == (
        reg.AUTH_FILE_NAME,
        reg.TOKENS_DIR_NAME,
    )
    assert config.WITHDRAWN_FILE_NAME == reg.WITHDRAWN_FILE_NAME
    assert auth.sha256_hex("токен") == reg.sha256_hex("токен")
    assert (auth.AGENT_NAME_PATTERN.pattern, auth.DIGEST_PATTERN.pattern) == (
        reg.AGENT_NAME_PATTERN,
        reg.DIGEST_PATTERN,
    )
