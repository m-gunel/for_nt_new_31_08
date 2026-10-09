"""Число потоков инференса должно определяться само, без переменных окружения.

Значение зависит от числа ядер машины, поэтому проверяется правило, а не число.
"""

import importlib
import logging

import pytest

import src.utils.openvino_encoder as encoder


def reload_with(monkeypatch: pytest.MonkeyPatch, value: str | None):
    monkeypatch.delenv("OV_NUM_THREADS", raising=False)
    if value is not None:
        monkeypatch.setenv("OV_NUM_THREADS", value)
    return importlib.reload(encoder)


def test_default_is_half_of_cores(monkeypatch: pytest.MonkeyPatch):
    module = reload_with(monkeypatch, None)
    assert module._resolve_num_threads() == module.DEFAULT_INFERENCE_THREADS


def test_default_never_below_two(monkeypatch: pytest.MonkeyPatch):
    """На одно- и двухъядерных машинах половина не должна выродиться в 1."""
    monkeypatch.setattr(encoder.os, "cpu_count", lambda: 2)
    module = reload_with(monkeypatch, None)
    assert module.DEFAULT_INFERENCE_THREADS >= 2


def test_env_overrides_default(monkeypatch: pytest.MonkeyPatch):
    module = reload_with(monkeypatch, "3")
    assert module._resolve_num_threads() == 3


def test_zero_returns_decision_to_plugin(monkeypatch: pytest.MonkeyPatch):
    """Ноль — явный отказ от лимита: решение отдаётся плагину OpenVINO."""
    module = reload_with(monkeypatch, "0")
    assert module._resolve_num_threads() is None


def test_garbage_does_not_crash(monkeypatch: pytest.MonkeyPatch):
    module = reload_with(monkeypatch, "шесть")
    assert module._resolve_num_threads() is None


@pytest.fixture(autouse=True)
def _restore_module():
    yield
    importlib.reload(encoder)


def test_streams_default_matches_channel_count():
    """По умолчанию streams ровно два — по числу каналов search и index.

    Один stream сериализует каналы: поисковый запрос ждёт окончания текущего
    батча индексации.
    """
    from src.utils.openvino_encoder import _resolve_num_streams

    assert _resolve_num_streams() == 2


def test_streams_env_overrides_default(monkeypatch: pytest.MonkeyPatch):
    """Ручка нужна, чтобы перемерить на АРМ без пересборки дистрибутива."""
    from src.utils.openvino_encoder import _resolve_num_streams

    monkeypatch.setenv("RAG_OV_NUM_STREAMS", "3")
    assert _resolve_num_streams() == 3


def test_streams_zero_returns_decision_to_plugin(monkeypatch: pytest.MonkeyPatch):
    from src.utils.openvino_encoder import _resolve_num_streams

    monkeypatch.setenv("RAG_OV_NUM_STREAMS", "0")
    assert _resolve_num_streams() is None


def test_streams_garbage_does_not_crash(monkeypatch: pytest.MonkeyPatch):
    from src.utils.openvino_encoder import _resolve_num_streams

    monkeypatch.setenv("RAG_OV_NUM_STREAMS", "две штуки")
    assert _resolve_num_streams() is None


@pytest.mark.parametrize("variable", ["RAG_OV_NUM_THREADS", "OV_NUM_THREADS"])
def test_garbage_warning_names_the_variable_it_came_from(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, variable: str
):
    """Предупреждение называет ту переменную, из которой пришло значение:
    иначе при неверной каноничной RAG_OV_NUM_THREADS ищут не ту."""
    monkeypatch.delenv("RAG_OV_NUM_THREADS", raising=False)
    monkeypatch.delenv("OV_NUM_THREADS", raising=False)
    monkeypatch.setenv(variable, "шесть")
    caplog.set_level(logging.WARNING)
    assert encoder._resolve_num_threads() is None
    assert [r.getMessage().split("=")[0] for r in caplog.records] == [variable]
