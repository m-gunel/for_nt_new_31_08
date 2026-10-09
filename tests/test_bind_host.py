"""Правило выбора адреса бинда: loopback по умолчанию, сеть — двумя переключателями.

Проверяется чистая функция, а не живой демон: правило обязано работать
одинаково при любом способе запуска, а старт демона — это прогрев OpenVINO.
"""

import pytest

from src.bind_host import EXPOSE_ENV, HOST_ENV, REFUSAL_EXIT_CODE, resolve_bind_host


def test_default_is_loopback() -> None:
    assert resolve_bind_host({}) == ("127.0.0.1", False)


def test_empty_host_is_default() -> None:
    """Пустой RAG_HOST — умолчание, а не «все интерфейсы» и не отказ старта."""
    assert resolve_bind_host({HOST_ENV: ""}) == ("127.0.0.1", False)
    assert resolve_bind_host({HOST_ENV: "  ", EXPOSE_ENV: "1"}) == ("127.0.0.1", False)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "localhost", "LOCALHOST"])
def test_explicit_loopback_quiet(host: str) -> None:
    assert resolve_bind_host({HOST_ENV: host}) == (host, False)


@pytest.mark.parametrize("host", ["0.0.0.0", "10.1.2.3", "rag.example", "::1"])
def test_external_without_flag_refuses(host: str) -> None:
    # ::1 тоже: сокет демона — AF_INET, IPv6-адрес на нём не забиндится.
    with pytest.raises(SystemExit) as exc:
        resolve_bind_host({HOST_ENV: host})

    assert exc.value.code == REFUSAL_EXIT_CODE


def test_external_with_flag_allowed() -> None:
    assert resolve_bind_host({HOST_ENV: "0.0.0.0", EXPOSE_ENV: "1"}) == (
        "0.0.0.0",
        True,
    )


def test_expose_flag_alone_keeps_loopback() -> None:
    # LQRAG_EXPOSE=1 только подтверждает решение открыть сеть, но не принимает его.
    assert resolve_bind_host({EXPOSE_ENV: "1"}) == ("127.0.0.1", False)


def test_refusal_names_host_and_switches(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        resolve_bind_host({HOST_ENV: "10.1.2.3"})

    err = capsys.readouterr().err
    # Иначе отказ читается как загадочный краш, а не как осознанный запрет.
    assert "10.1.2.3" in err
    assert HOST_ENV in err and EXPOSE_ENV in err and "RAG_ALLOWED_HOSTS" in err
