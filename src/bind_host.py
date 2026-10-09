"""Выбор адреса бинда демона: loopback по умолчанию, сеть — двумя переключателями.

API отдаёт тексты личных файлов, поэтому один забытый env не должен открывать
его сети: не-loopback RAG_HOST действует только вместе с LQRAG_EXPOSE=1, иначе
демон отказывается стартовать и объясняет почему. Проверка Host в
loopback_guard сетевой границей не служит: удалённый клиент подставит любой Host.

Модуль лёгкий (только stdlib): правило тестируется без импорта main.py и его
цепочки OpenVINO/Lance.
"""

import ipaddress
import sys
from collections.abc import Mapping

HOST_ENV = "RAG_HOST"
EXPOSE_ENV = "LQRAG_EXPOSE"
DEFAULT_HOST = "127.0.0.1"
REFUSAL_EXIT_CODE = 2

_EXPOSE_ON = "1"
_LOCALHOST = "localhost"

_REFUSAL = (
    "ОТКАЗ: RAG_HOST=%s — не loopback-адрес IPv4 (127.0.0.0/8, localhost): "
    "API индексатора был бы доступен по сети.\n"
    "Для локальной работы уберите %s или оставьте его пустым. Если доступ по "
    "сети осознан, задайте одновременно %s=1, а имена, по которым к демону "
    "обращаются, — в RAG_ALLOWED_HOSTS. Клиентам по сети нужен токен агента, "
    "и по HTTP он идёт открытым текстом.\n"
)


def _is_loopback(host: str) -> bool:
    # Только IPv4: сокет демона — AF_INET, ::1 на нём не забиндится.
    if host.lower() == _LOCALHOST:
        return True
    try:
        return ipaddress.IPv4Address(host).is_loopback
    except ValueError:
        return False


def resolve_bind_host(env: Mapping[str, str]) -> tuple[str, bool]:
    """(адрес бинда, слушаем ли не-loopback) по переданному окружению.

    Пустой RAG_HOST — умолчание, а не «все интерфейсы». При exposed=True
    вызывающий обязан громко предупредить в логе. Не-loopback без
    LQRAG_EXPOSE=1 — текст отказа в stderr и SystemExit(REFUSAL_EXIT_CODE).
    """
    host = (env.get(HOST_ENV) or "").strip() or DEFAULT_HOST
    exposed = not _is_loopback(host)
    if exposed and env.get(EXPOSE_ENV) != _EXPOSE_ON:
        sys.stderr.write(_REFUSAL % (host, HOST_ENV, EXPOSE_ENV))
        raise SystemExit(REFUSAL_EXIT_CODE)
    return host, exposed
