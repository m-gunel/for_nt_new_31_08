"""Bearer-токен агента на входе в API (VAS-6850).

API отдаёт тексты личных файлов, а loopback-бинд не отсекает локальные процессы
других учёток. Регистратор (scripts/register_mcp.py) кладёт агенту токен в
~/.files-search/tokens/<агент>.token (0600), демону — только sha256 в
~/.files-search/auth.json; демон читает <lsm_root>/auth.json (при DEV=1 —
data/app). Fail-closed: файла нет, он пуст, битый или чужой — 401 всем.
Процессы той же учётки токены прочитают: это граница ОС, а не модуля.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import stat
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from time import monotonic
from types import MappingProxyType
from typing import NamedTuple

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from src.config import AUTH_FILE_NAME
from src.utils import AppConfig, get_config, get_logger

lg = get_logger(__name__)

# /health нужна мосту, чтобы отличить «демон не поднялся» от «нужен токен»;
# данных индекса она не отдаёт.
OPEN_PATHS = frozenset({"/api/v1/health"})

# Отдают тексты файлов или меняют состояние — в аудит на INFO. Остальное
# (опрос /status мостом) — на DEBUG, чтобы не забивать журнал.
AUDITED_ROUTES = frozenset(
    {
        ("POST", "/api/v1/search"),
        ("POST", "/api/v1/read_file"),
        ("POST", "/api/v1/config"),
        ("POST", "/api/v1/activate"),
        ("POST", "/api/v1/deactivate"),
        ("POST", "/api/v1/reset"),
    }
)

AGENT_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")

UNAUTHORIZED_DETAIL = "требуется токен агента"
# Перебор токенов не должен забивать журнал: об отказах — строка за интервал.
REJECT_LOG_INTERVAL_S = 60.0
# Сбой ОС при чтении auth.json (EMFILE, EIO) не запоминается до смены файла —
# иначе 401 всем до перезапуска, — но и не повторяется на каждом запросе.
READ_RETRY_S = 10.0

# Писать в auth.json может только владелец: иначе чужой вписал бы свой хэш.
_FOREIGN_WRITE_BITS = stat.S_IWGRP | stat.S_IWOTH
_MAX_NAME_IN_LOG = 40
_REGISTER_HINT = "register_mcp.py --all"
_NO_AGENTS: Mapping[str, str] = MappingProxyType({})


class _FileStamp(NamedTuple):
    """Признаки смены auth.json без его чтения.

    Регистратор пишет через os.replace — inode новый, даже если размер и тик
    mtime прежние (--rotate меняет только хэши). Владелец и права — чтобы
    исправленный chmod открыл доступ без перезапуска.
    """

    ino: int
    mtime_ns: int
    size: int
    uid: int
    mode: int


# Такого штампа у файла не бывает: первое обращение всегда читает файл.
_NEVER_READ = _FileStamp(-1, -1, -1, -1, -1)


def sha256_hex(token: str) -> str:
    """Хэш токена для auth.json: сам токен демону хранить незачем."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def bearer_token(header_value: str | None) -> str | None:
    """Токен из «Authorization: Bearer <t>» либо None.

    Схема без учёта регистра, как в RFC 7235: иначе рабочий curl получил бы
    необъяснимый 401.
    """
    if not header_value:
        return None
    parts = header_value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def match_agent(agents: Mapping[str, str], token: str | None) -> str | None:
    """Имя агента, чей хэш совпал с хэшем токена, либо None.

    compare_digest, а не поиск по словарю: время сравнения не подсказывает,
    сколько символов хэша угадано.
    """
    if not token:
        return None
    presented = sha256_hex(token)
    for name, digest in agents.items():
        if hmac.compare_digest(presented, digest):
            return name
    return None


def _is_valid_entry(name: object, digest: object) -> bool:
    # Хэш не из ASCII уронил бы compare_digest (TypeError → 500 вместо 401).
    return (
        isinstance(name, str)
        and isinstance(digest, str)
        and AGENT_NAME_PATTERN.fullmatch(name) is not None
        and DIGEST_PATTERN.fullmatch(digest) is not None
    )


def _parse_agents(path: Path) -> Mapping[str, str]:
    """{агент: sha256} из auth.json; недопустимые записи пропускаются с warning.

    OSError чтения не ловит: его повторяет AuthStore.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, RecursionError) as exc:
        lg.warning("авторизация: %s не разобран: %s", path, exc)
        return _NO_AGENTS
    if not isinstance(data, dict):
        lg.warning("авторизация: в %s не объект JSON", path)
        return _NO_AGENTS
    agents = {
        name: digest for name, digest in data.items() if _is_valid_entry(name, digest)
    }
    skipped = sorted(
        repr(name)[:_MAX_NAME_IN_LOG] for name in data.keys() - agents.keys()
    )
    if skipped:
        lg.warning(
            "авторизация: в %s пропущены записи с недопустимым именем агента "
            "или хэшем: %s",
            path,
            ", ".join(skipped),
        )
    return MappingProxyType(agents)


class AuthStore:
    """Агенты из <root>/auth.json; файл перечитывается, только когда сменился штамп.

    В лог пишет только при смене файла (нет, чужой, битый, загружен) и при
    сбое чтения — не чаще раза в READ_RETRY_S, а не на каждый запрос.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._path = Path(root).expanduser() / AUTH_FILE_NAME
        # verify_token идёт в пуле потоков: штамп и агенты меняются только
        # вместе, иначе отозванный токен прошёл бы с новым штампом.
        self._lock = threading.Lock()
        self._snapshot: tuple[_FileStamp | None, Mapping[str, str]] = (
            _NEVER_READ,
            _NO_AGENTS,
        )
        self._retry_at = float("inf")

    def agents(self) -> Mapping[str, str]:
        with self._lock:
            stamp = self._stamp()
            if stamp != self._snapshot[0] or monotonic() >= self._retry_at:
                self._retry_at = float("inf")
                try:
                    agents = self._load(stamp)
                except OSError as exc:
                    lg.warning(
                        "авторизация: %s не прочитан: %s — доступ закрыт, повтор "
                        "через %.0f с",
                        self._path,
                        exc,
                        READ_RETRY_S,
                    )
                    agents = _NO_AGENTS
                    self._retry_at = monotonic() + READ_RETRY_S
                self._snapshot = (stamp, agents)
            return self._snapshot[1]

    def _stamp(self) -> _FileStamp | None:
        try:
            # lstat: симлинк не принимаем, как и регистратор (O_NOFOLLOW).
            st = os.lstat(self._path)
        except OSError:
            # Нет файла — штатное состояние до регистрации; пишет его _load.
            return None
        return _FileStamp(st.st_ino, st.st_mtime_ns, st.st_size, st.st_uid, st.st_mode)

    def _load(self, stamp: _FileStamp | None) -> Mapping[str, str]:
        if stamp is None:
            lg.warning(
                "авторизация: %s нет или недоступен — доступ к API закрыт до "
                "регистрации агентов: %s",
                self._path,
                _REGISTER_HINT,
            )
            return _NO_AGENTS
        if not stat.S_ISREG(stamp.mode):
            lg.warning(
                "авторизация: %s — не обычный файл (симлинк?); доступ к API "
                "закрыт до регистрации: %s",
                self._path,
                _REGISTER_HINT,
            )
            return _NO_AGENTS
        if stamp.uid != os.getuid() or stamp.mode & _FOREIGN_WRITE_BITS:
            lg.warning(
                "авторизация: %s принадлежит uid %d с правами %03o — нужен "
                "владелец uid %d и запись только для него (chmod 600); доступ "
                "к API закрыт",
                self._path,
                stamp.uid,
                stat.S_IMODE(stamp.mode),
                os.getuid(),
            )
            return _NO_AGENTS
        agents = _parse_agents(self._path)
        if agents:
            lg.info("авторизация: агенты %s", ", ".join(sorted(agents)))
        else:
            lg.warning(
                "авторизация: в %s нет действующих агентов — доступ к API закрыт "
                "до регистрации: %s",
                self._path,
                _REGISTER_HINT,
            )
        return agents


_stores: dict[str, AuthStore] = {}
_stores_lock = threading.Lock()


def _store_for(root: str | os.PathLike[str]) -> AuthStore:
    """Один AuthStore на каталог: штамп и уже записанные в лог состояния живут
    весь срок демона."""
    key = str(Path(root).expanduser())
    with _stores_lock:
        store = _stores.get(key)
        if store is None:
            store = _stores[key] = AuthStore(key)
        return store


def log_auth_state(root: str | os.PathLike[str]) -> None:
    """Прочитать auth.json при старте: причина будущих 401 видна в логе сразу."""
    _store_for(root).agents()


class _RejectionLog:
    """Отказы 401 в лог не чаще раза в интервал, с числом пропущенных."""

    def __init__(
        self, interval_s: float, clock: Callable[[], float] = monotonic
    ) -> None:
        self._interval_s = interval_s
        self._clock = clock
        self._lock = threading.Lock()
        self._next_at = float("-inf")
        self._suppressed = 0

    def record(self, request_line: str, reason: str) -> None:
        now = self._clock()
        with self._lock:
            if now < self._next_at:
                self._suppressed += 1
                return
            suppressed, self._suppressed = self._suppressed, 0
            self._next_at = now + self._interval_s
        lg.warning(
            "отказ 401: %s — %s (ещё отказов с прошлой записи: %d)",
            request_line,
            reason,
            suppressed,
        )


_rejections = _RejectionLog(REJECT_LOG_INTERVAL_S)


def _rejection_reason(agents: Mapping[str, str], token: str | None) -> str:
    if not agents:
        return "нет зарегистрированных агентов (%s)" % _REGISTER_HINT
    if token is None:
        return "нет заголовка Authorization: Bearer"
    return "токен не подошёл"


def _audit(agent: str, method: str, path: str) -> None:
    level = logging.INFO if (method, path) in AUDITED_ROUTES else logging.DEBUG
    lg.log(level, "доступ: агент=%s %s %s", agent, method, path)


def _authenticate(root: str, method: str, path: str, header: str | None) -> str | None:
    """Имя агента по заголовку Authorization; None — отказ, он уже в логе."""
    token = bearer_token(header)
    agents = _store_for(root).agents()
    agent = match_agent(agents, token)
    if agent is None:
        _rejections.record("%s %s" % (method, path), _rejection_reason(agents, token))
    return agent


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=UNAUTHORIZED_DETAIL,
        headers={"WWW-Authenticate": "Bearer"},
    )


class TokenFirstMiddleware:
    """401 раньше, чем прочитано тело запроса.

    FastAPI читает и разбирает тело до зависимостей роутера: без этого запрос
    без токена копил бы в памяти демона тело любого размера, а на битый JSON
    получал бы 422 вместо 401. verify_token остаётся: подписывает запрос
    именем агента и пишет аудит.
    """

    def __init__(self, app: ASGIApp, paths: frozenset[str]) -> None:
        self.app = app
        # Ручки роутера: неизвестный путь — 404, как без прослойки.
        self.paths = paths - OPEN_PATHS

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] in self.paths:
            header = Headers(scope=scope).get("authorization")
            agent = await run_in_threadpool(
                _authenticate,
                get_config().lsm_root,
                scope["method"],
                scope["path"],
                header,
            )
            if agent is None:
                denied = _unauthorized()
                response = JSONResponse(
                    {"detail": denied.detail},
                    status_code=denied.status_code,
                    headers=denied.headers,
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def verify_token(request: Request, cfg: AppConfig) -> None:
    """Зависимость роутера /api/v1: пускать только носителей действующего токена.

    Синхронная: stat и чтение мелкого файла FastAPI уводит в пул потоков.
    Ответ 401 один для «нет токена» и «чужой токен»; причина — только в логе.
    """
    path = request.url.path
    if path in OPEN_PATHS:
        return
    agent = _authenticate(
        cfg.lsm_root, request.method, path, request.headers.get("Authorization")
    )
    if agent is None:
        raise _unauthorized()
    # Имя — из совпавшего хэша, а не из заголовков: их клиент выбирает сам.
    request.state.agent = agent
    _audit(agent, request.method, path)
