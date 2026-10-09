"""Аутентификация API: хэши из auth.json, bearer-заголовок, fail-closed.

Чистые функции, AuthStore и verify_token проверяются на tmp-каталоге и фейках
Request/конфига. Подключение к настоящему приложению — через TestClient без
lifespan: модель не грузится, ручки до побочных эффектов не доходят.
"""

import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient

import src.server.auth as auth
from src.indexer.utils import _get_index
from src.server import app
from src.server.auth import (
    OPEN_PATHS,
    REJECT_LOG_INTERVAL_S,
    UNAUTHORIZED_DETAIL,
    AuthStore,
    bearer_token,
    log_auth_state,
    match_agent,
    sha256_hex,
    verify_token,
)

AUTH_LOGGER = "app.server.auth"
# Редиректы на открытую документацию (/docs, /openapi.json): данных не отдают.
DOC_ALIASES = frozenset({"/api/v1/docs", "/api/v1/openapi.json"})


def _write_auth(root: Path, tokens: dict[str, str]) -> Path:
    """auth.json {агент: sha256(токен)} — как пишет регистратор: tmp + os.replace."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / "auth.json"
    tmp = root / "auth.json.tmp"
    tmp.write_text(
        json.dumps({name: sha256_hex(token) for name, token in tokens.items()}),
        encoding="utf-8",
    )
    tmp.chmod(0o600)
    os.replace(tmp, path)
    return path


def _write_raw(root: Path, data: object) -> Path:
    path = root / "auth.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(0o600)
    return path


class _Request:
    """Минимальный Request: только то, что читает verify_token."""

    def __init__(
        self,
        authorization: str | None = None,
        path: str = "/api/v1/search",
        method: str = "POST",
    ) -> None:
        self.headers = {} if authorization is None else {"Authorization": authorization}
        self.method = method
        self.url = SimpleNamespace(path=path)
        self.state = SimpleNamespace()


class _Config:
    def __init__(self, lsm_root: Path) -> None:
        self.lsm_root = str(lsm_root)


def _rejected(request: _Request, root: Path) -> HTTPException:
    with pytest.raises(HTTPException) as exc:
        verify_token(request, _Config(root))
    return exc.value


def _messages(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == level]


def _audit_lines(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [m for m in _messages(caplog, level) if m.startswith("доступ:")]


# --- разбор заголовка и сопоставление ----------------------------------------


def test_bearer_parse() -> None:
    assert bearer_token("Bearer abc") == "abc"
    assert bearer_token("bearer abc") == "abc"  # схема регистронезависима
    assert bearer_token("  Bearer   a b  ") == "a b"  # токен с пробелами — цельный
    assert bearer_token("") is None
    assert bearer_token(None) is None
    assert bearer_token("Basic abc") is None  # чужая схема — не токен
    assert bearer_token("Bearer") is None
    assert bearer_token("Bearer   ") is None


def test_match_agent_by_hash(tmp_path: Path) -> None:
    _write_auth(tmp_path, {"gigacode": "T1", "wsc": "T2"})

    agents = AuthStore(tmp_path).agents()

    assert match_agent(agents, "T1") == "gigacode"
    assert match_agent(agents, "T2") == "wsc"
    assert match_agent(agents, "угадал-в-миллионный-раз") is None
    assert match_agent(agents, "") is None
    assert match_agent({}, "T1") is None


# --- AuthStore: перечитывание и fail-closed ----------------------------------


def test_store_reloads_when_auth_changes(tmp_path: Path) -> None:
    store = AuthStore(tmp_path)
    _write_auth(tmp_path, {"gigacode": "T1"})
    assert set(store.agents()) == {"gigacode"}

    _write_auth(tmp_path, {"gigacode": "T1", "wsc": "T2"})
    assert set(store.agents()) == {"gigacode", "wsc"}

    # Отзыв: агент исчез из файла — его токен перестаёт действовать сразу.
    _write_auth(tmp_path, {"wsc": "T2"})
    agents = store.agents()
    assert set(agents) == {"wsc"}
    assert match_agent(agents, "T1") is None


def test_store_rereads_replaced_file_of_same_size_and_mtime(tmp_path: Path) -> None:
    """--rotate меняет только хэши: размер прежний, mtime может попасть в тот же тик."""
    path = _write_auth(tmp_path, {"gigacode": "T1"})
    store = AuthStore(tmp_path)
    assert match_agent(store.agents(), "T1") == "gigacode"
    before = path.stat()
    _write_auth(tmp_path, {"gigacode": "T2"})
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_size == before.st_size

    agents = store.agents()

    assert match_agent(agents, "T2") == "gigacode"
    assert match_agent(agents, "T1") is None


def _fail_reads(monkeypatch: pytest.MonkeyPatch, times: int) -> list[int]:
    """Первые times чтений auth.json падают с EMFILE; вернуть счётчик попыток."""
    real_read = Path.read_text
    attempts = [0]

    def flaky(self: Path, *args, **kwargs) -> str:
        attempts[0] += 1
        if attempts[0] <= times:
            raise OSError(24, "Too many open files")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky)
    return attempts


def test_store_rereads_after_transient_read_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EMFILE/EIO при чтении не запоминается: иначе 401 всем до смены файла."""
    _write_auth(tmp_path, {"gigacode": "T1"})
    store = AuthStore(tmp_path)
    _fail_reads(monkeypatch, times=1)
    monkeypatch.setattr(auth, "READ_RETRY_S", 0.0)

    assert store.agents() == {}
    assert match_agent(store.agents(), "T1") == "gigacode"


def test_store_read_error_is_retried_not_on_every_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Постоянный сбой (права 000) — не строка WARNING на каждый опрос /status."""
    _write_auth(tmp_path, {"gigacode": "T1"})
    store = AuthStore(tmp_path)
    attempts = _fail_reads(monkeypatch, times=100)

    with caplog.at_level("WARNING"):
        for _ in range(5):
            assert store.agents() == {}

    assert attempts == [1]
    assert caplog.text.count("не прочитан") == 1


def test_store_symlink_is_fail_closed(tmp_path: Path) -> None:
    """Как и регистратор (O_NOFOLLOW): auth.json — только обычный файл."""
    target = _write_auth(tmp_path / "elsewhere", {"gigacode": "T1"})
    root = tmp_path / "root"
    root.mkdir()
    (root / "auth.json").symlink_to(target)

    assert AuthStore(root).agents() == {}


def test_store_missing_file_is_fail_closed(tmp_path: Path) -> None:
    assert AuthStore(tmp_path).agents() == {}


def test_store_missing_file_warned_once_with_hint(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = AuthStore(tmp_path)

    with caplog.at_level(logging.WARNING, logger=AUTH_LOGGER):
        store.agents()
        store.agents()

    (warning,) = _messages(caplog, logging.WARNING)
    assert "register_mcp.py --all" in warning


def test_store_warns_again_when_file_disappears(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write_auth(tmp_path, {"gigacode": "T1"})
    store = AuthStore(tmp_path)
    store.agents()
    path.unlink()

    with caplog.at_level(logging.WARNING, logger=AUTH_LOGGER):
        agents = store.agents()

    assert agents == {}
    assert len(_messages(caplog, logging.WARNING)) == 1


def test_log_auth_state_reports_missing_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Старт демона без регистрации: причина будущих 401 видна в логе сразу."""
    with caplog.at_level(logging.WARNING, logger=AUTH_LOGGER):
        log_auth_state(tmp_path / "fresh")

    assert "доступ к API закрыт" in caplog.text


def test_store_broken_json_is_fail_closed(tmp_path: Path) -> None:
    _write_auth(tmp_path, {"gigacode": "T1"})
    store = AuthStore(tmp_path)
    assert store.agents()

    (tmp_path / "auth.json").write_text("{битый json", encoding="utf-8")
    os.utime(tmp_path / "auth.json", ns=(0, 0))
    assert store.agents() == {}

    # Починка файла возвращает доступ без перезапуска демона.
    _write_auth(tmp_path, {"gigacode": "T1"})
    assert set(store.agents()) == {"gigacode"}


def test_store_ignores_non_string_entries(tmp_path: Path) -> None:
    _write_raw(tmp_path, {"ok": sha256_hex("T"), "bad": None, "bad2": 7})

    assert set(AuthStore(tmp_path).agents()) == {"ok"}


@pytest.mark.parametrize(
    "digest",
    ["я" * 64, sha256_hex("T").upper(), sha256_hex("T")[:-1], "T"],
    ids=["cyrillic", "upper-hex", "short", "plain-token"],
)
def test_malformed_digest_gives_401_not_500(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, digest: str
) -> None:
    """Хэш не по контракту пропускается: compare_digest на не-ASCII бросил бы TypeError."""
    _write_raw(tmp_path, {"gigacode": digest})

    with caplog.at_level(logging.WARNING, logger=AUTH_LOGGER):
        error = _rejected(_Request("Bearer T"), tmp_path)

    assert error.status_code == 401
    assert "'gigacode'" in caplog.text


@pytest.mark.parametrize("name", ["../evil", "Upper", "-dash", "a" * 33, ""])
def test_agent_name_outside_contract_is_skipped(tmp_path: Path, name: str) -> None:
    _write_raw(tmp_path, {name: sha256_hex("T"), "wsc": sha256_hex("T2")})

    assert set(AuthStore(tmp_path).agents()) == {"wsc"}


def test_group_writable_file_closes_access_until_chmod(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write_auth(tmp_path, {"gigacode": "T1"})
    path.chmod(0o620)
    store = AuthStore(tmp_path)

    with caplog.at_level(logging.WARNING, logger=AUTH_LOGGER):
        assert store.agents() == {}
    assert "chmod 600" in caplog.text

    # Права исправлены без перезаписи файла — доступ возвращается сразу.
    path.chmod(0o600)
    assert set(store.agents()) == {"gigacode"}


def test_foreign_owner_closes_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_auth(tmp_path, {"gigacode": "T1"})
    foreign_uid = os.getuid() + 1
    monkeypatch.setattr(auth.os, "getuid", lambda: foreign_uid)

    assert AuthStore(tmp_path).agents() == {}


# --- зависимость verify_token ------------------------------------------------


def test_verify_token_passes_and_signs_request(tmp_path: Path) -> None:
    _write_auth(tmp_path, {"gigacode": "T1"})
    req = _Request("Bearer T1")

    verify_token(req, _Config(tmp_path))

    assert req.state.agent == "gigacode"


def test_verify_token_rejects_without_header(tmp_path: Path) -> None:
    _write_auth(tmp_path, {"gigacode": "T1"})

    assert _rejected(_Request(), tmp_path).status_code == 401


def test_verify_token_rejects_unknown_token(tmp_path: Path) -> None:
    _write_auth(tmp_path, {"gigacode": "T1"})

    assert _rejected(_Request("Bearer чужой"), tmp_path).status_code == 401


def test_verify_token_fail_closed_without_auth_file(tmp_path: Path) -> None:
    assert _rejected(_Request("Bearer что-угодно"), tmp_path).status_code == 401


def test_rejection_is_identical_for_missing_and_wrong_token(tmp_path: Path) -> None:
    """Причину отказа клиент не узнаёт: переборщику не подсказываем."""
    _write_auth(tmp_path, {"gigacode": "T1"})

    missing = _rejected(_Request(), tmp_path)
    wrong = _rejected(_Request("Bearer чужой"), tmp_path)

    for error in (missing, wrong):
        assert error.detail == UNAUTHORIZED_DETAIL
        assert error.headers == {"WWW-Authenticate": "Bearer"}


def test_health_stays_open_without_token(tmp_path: Path) -> None:
    req = _Request(path="/api/v1/health", method="GET")

    verify_token(req, _Config(tmp_path))

    assert not hasattr(req.state, "agent")


@pytest.mark.parametrize(
    "method, path, level",
    [
        ("POST", "/api/v1/search", logging.INFO),
        ("POST", "/api/v1/read_file", logging.INFO),
        ("POST", "/api/v1/config", logging.INFO),
        ("POST", "/api/v1/reset", logging.INFO),
        ("GET", "/api/v1/status", logging.DEBUG),
        ("GET", "/api/v1/config", logging.DEBUG),
    ],
)
def test_audit_info_only_for_texts_and_state_changes(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    method: str,
    path: str,
    level: int,
) -> None:
    _write_auth(tmp_path, {"gigacode": "T1"})

    with caplog.at_level(logging.DEBUG, logger=AUTH_LOGGER):
        verify_token(_Request("Bearer T1", path, method), _Config(tmp_path))

    expected = "доступ: агент=gigacode %s %s" % (method, path)
    assert _audit_lines(caplog, level) == [expected]


def test_rejections_logged_once_per_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _write_auth(tmp_path, {"gigacode": "T1"})
    now = [1000.0]
    throttle = auth._RejectionLog(REJECT_LOG_INTERVAL_S, clock=lambda: now[0])
    monkeypatch.setattr(auth, "_rejections", throttle)

    with caplog.at_level(logging.WARNING, logger=AUTH_LOGGER):
        for _ in range(3):
            _rejected(_Request("Bearer чужой-токен"), tmp_path)
        now[0] += REJECT_LOG_INTERVAL_S
        _rejected(_Request(), tmp_path)

    first, second = _messages(caplog, logging.WARNING)
    assert "POST /api/v1/search" in first and "токен не подошёл" in first
    assert "нет заголовка" in second and "прошлой записи: 2" in second
    assert "чужой-токен" not in caplog.text, "токен попал в лог"


# --- подключение к настоящему приложению -------------------------------------


@pytest.fixture
def client() -> TestClient:
    # Без with: lifespan (прогрев модели, индексатор) не запускается.
    return TestClient(app)


def _guarded_operations() -> list[tuple[str, str]]:
    """(метод, путь) всех ручек /api/v1, кроме открытых, из app.routes."""
    return sorted(
        (method, route.path)
        for route in iter_route_contexts(app.routes)
        if route.path
        and route.path.startswith("/api/v1/")
        and route.path not in OPEN_PATHS | DOC_ALIASES
        for method in route.methods or ()
    )


def _index_must_stay_untouched() -> None:
    raise AssertionError("ручка вызвана без токена")


def test_every_api_route_requires_token(
    client: TestClient, agent_auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Новая ручка /api/v1 без проверки токена — провал теста, а не тихая дыра."""
    # Если проверку всё же забыли, ручка не доберётся до индекса.
    monkeypatch.setitem(
        app.dependency_overrides, _get_index, _index_must_stay_untouched
    )
    operations = _guarded_operations()

    statuses = {op: client.request(*op).status_code for op in operations}

    assert ("POST", "/api/v1/read_file") in statuses
    assert statuses == dict.fromkeys(operations, 401)


def test_app_rejects_request_without_token(
    client: TestClient, agent_auth: dict[str, str]
) -> None:
    resp = client.get("/api/v1/status")

    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    assert resp.json() == {"detail": UNAUTHORIZED_DETAIL}


def test_app_checks_token_before_body_validation(
    client: TestClient, agent_auth: dict[str, str]
) -> None:
    """Без токена клиент не узнаёт даже схему тела: 401, а не 422."""
    resp = client.post(
        "/api/v1/read_file", json={}, headers={"Authorization": "Bearer not-ours"}
    )

    assert resp.status_code == 401


@pytest.mark.parametrize(
    "body", [b"{not json", b"x" * 100_000], ids=["битый-json", "большое-тело"]
)
def test_app_rejects_before_reading_body(
    client: TestClient, agent_auth: dict[str, str], body: bytes
) -> None:
    """Без токена тело не разбирается: 401, а не 422."""
    resp = client.post(
        "/api/v1/search",
        content=body,
        headers={"Content-Type": "application/json"},
    )

    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    assert resp.json() == {"detail": UNAUTHORIZED_DETAIL}


def test_app_unknown_api_path_stays_404(
    client: TestClient, agent_auth: dict[str, str]
) -> None:
    assert client.get("/api/v1/no-such-route").status_code == 404


def test_app_accepts_registered_token(
    client: TestClient, agent_auth: dict[str, str]
) -> None:
    # 422 — проверка пройдена, отказ уже валидации тела, ручка не вызвана.
    resp = client.post("/api/v1/read_file", json={}, headers=agent_auth)

    assert resp.status_code == 422


def test_app_open_paths_need_no_token(
    client: TestClient, agent_auth: dict[str, str]
) -> None:
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    for alias in DOC_ALIASES:
        assert client.get(alias, follow_redirects=False).status_code == 307


def test_swagger_offers_bearer_authorization(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()

    assert schema["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    assert {"HTTPBearer": []} in schema["paths"]["/api/v1/search"]["post"]["security"]
