import os
from pathlib import Path

import pytest
from pytest import fixture

from src.utils.lsm_adapter import LSMTable

root = Path(__file__).parent


def _test_encoder(data: str) -> bytes:
    return data.encode("utf-8")


def _test_decoder(key: str, data: bytes) -> str:
    return data.decode("utf-8")


@fixture
def table():
    p = root / "tmp"
    os.makedirs(p, exist_ok=True)
    p /= "test.lsm"

    _table = LSMTable(
        path=p,
        encoder=_test_encoder,
        decoder=_test_decoder,
    )
    try:
        yield _table
        _table.close()
    finally:
        os.remove(p)


def test_table(table: LSMTable[str]):
    table.set("test", "value")
    value = table.get("test")
    assert value == "value"

    table.set("test2", "value2")
    res = set(table.items())
    assert res == {("test", "value"), ("test2", "value2")}

    table.reset()

    table.set("test3", "value3")
    res = set(table.items())
    assert res == {("test3", "value3")}

    table.set("test4", "value")
    table.set("other", "value")

    table.delete_prefix("test")
    res = set(table.items())

    assert res == {("other", "value")}


@pytest.mark.asyncio
async def test_table_async(table: LSMTable):
    await table.async_set("test", "value")
    value = await table.async_get("test")
    assert value == "value"

    await table.async_set("test2", "value2")
    res = set()
    async for key, value in table.async_items():
        res.add((key, value))

    assert res == {("test", "value"), ("test2", "value2")}
