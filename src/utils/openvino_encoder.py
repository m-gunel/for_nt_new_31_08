import asyncio
import concurrent
import concurrent.futures
import gc
import hashlib
import os
import threading
import time
from functools import cached_property, partial
from pathlib import Path
from typing import Literal

import numpy as np
import openvino as ov
import openvino.properties as op
import openvino_tokenizers  # noqa: F401  # регистрирует расширение токенизатора

from src.malloc_tuning import safe_malloc_trim
from src.utils.thread_priority import background_pool_initializer

from .logging import get_logger

lg = get_logger(__name__)

_cfg = {
    # LATENCY, а не THROUGHPUT: под THROUGHPUT плагин дробит пул на streams по
    # числу ядер и отдаёт каждому по одному потоку, что резко замедляет
    # индексацию. Нужны ровно два независимых канала — streams задаём явно.
    op.hint.performance_mode(): op.hint.PerformanceMode.LATENCY  # type: ignore
}

# Ровно по числу каналов инференса: search и index. С одним stream поисковый
# запрос ждёт конца батча индексации; с большим числом каждый stream получает
# по одному потоку, и индексация замедляется в разы.
DEFAULT_INFERENCE_STREAMS = 2

# Под LATENCY плагин отдаёт инференсу все ядра машины, а фоновый демон на
# рабочей станции так занимает её целиком. Модель крошечная, и на всех ядрах
# заметная часть процессорного времени уходит на конкуренцию потоков, а не на
# счёт, поэтому по умолчанию берём половину ядер. RAG_OV_NUM_THREADS (старое
# имя OV_NUM_THREADS) перекрывает дефолт, значение 0 возвращает решение плагину.
DEFAULT_INFERENCE_THREADS = max(2, (os.cpu_count() or 4) // 2)

# IR модели и токенизатора относительно каталога модели (.bin лежит рядом с .xml).
MODEL_IR = "openvino/openvino_model_qint8_quantized.xml"
TOKENIZER_IR = "openvino_tokenizer.xml"
# Длина отпечатка: 64 бита хватает, чтобы различать версии модели.
_FINGERPRINT_CHARS = 16

# Батч из batch_size фрагментов считается за доли секунды даже на слабом АРМ:
# вызов дольше — зависание, а не медленная машина (сторож systemd, src/server/app.py).
INFER_STALL_LIMIT_S = 300.0


def model_fingerprint(model_path: str | os.PathLike) -> str:
    """Отпечаток модели: sha256 IR модели и токенизатора (.xml и .bin).

    От них зависят векторы и нарезка — чанк считается токенами этого
    токенизатора, — поэтому индекс другой модели собирается заново.
    """
    digest = hashlib.sha256()
    for ir in (MODEL_IR, TOKENIZER_IR):
        xml = Path(model_path) / ir
        for part in (xml, xml.with_suffix(".bin")):
            with open(part, "rb") as f:
                digest.update(hashlib.file_digest(f, "sha256").digest())
    return digest.hexdigest()[:_FINGERPRINT_CHARS]


def _resolve_num_threads() -> int | None:
    """Сколько потоков отдать инференсу. None — решает плагин.

    RAG_OV_NUM_THREADS — каноничное имя (в проекте все переменные под
    префиксом RAG_); OV_NUM_THREADS остаётся для совместимости.
    """
    name = "RAG_OV_NUM_THREADS"
    if not os.environ.get(name):
        name = "OV_NUM_THREADS"
    requested = os.environ.get(name, "").strip()
    if not requested:
        return DEFAULT_INFERENCE_THREADS
    if requested.isdigit():
        value = int(requested)
        return value if value > 0 else None
    lg.warning("%s=%r — не число, лимит потоков не применён", name, requested)
    return None


def _resolve_num_streams() -> int | None:
    """Сколько streams отдать плагину. None — решает плагин сам.

    Отдельная ручка нужна, чтобы подобрать значение на целевой машине, не
    пересобирая дистрибутив.
    """
    requested = os.environ.get("RAG_OV_NUM_STREAMS", "").strip()
    if not requested:
        return DEFAULT_INFERENCE_STREAMS
    if requested.isdigit():
        value = int(requested)
        return value if value > 0 else None
    lg.warning("RAG_OV_NUM_STREAMS=%r — не число, число streams не задано", requested)
    return None


_num_threads = _resolve_num_threads()
if _num_threads is not None:
    _cfg[op.inference_num_threads()] = _num_threads  # type: ignore
    lg.info("потоков инференса: %s из %s ядер машины", _num_threads, os.cpu_count())
else:
    lg.info("потоков инференса: решает плагин (все ядра машины)")

_num_streams = _resolve_num_streams()
if _num_streams is not None:
    _cfg[op.num_streams()] = _num_streams  # type: ignore
    lg.info("streams инференса: %s", _num_streams)
else:
    lg.info("streams инференса: решает плагин")

Role = Literal["search", "index"]


class _InferChannel:
    """Независимый канал инференса: своя пара infer-request'ов и свой поток.

    У поиска и индексации свои каналы: на общем пуле живой поисковый запрос
    встаёт в очередь за батчами фоновой индексации.
    """

    def __init__(
        self,
        name: str,
        model: ov.CompiledModel,
        tokenizer: ov.CompiledModel,
        reset_memory_every_n: int = 100,
    ) -> None:
        self.name = name
        self.model = model
        self.tokenizer = tokenizer
        self.reset_memory_every_n = reset_memory_every_n
        self._tok_inf: ov.InferRequest | None = None
        self._model_inf: ov.InferRequest | None = None
        self._lock = threading.Lock()
        self._counter = 0
        # Начало идущего вызова (monotonic) — для сторожа; None — канал свободен.
        # Без лока: сторож читает его из event loop, а лок канала держит сам
        # (возможно, зависший) инференс; чтение float под GIL атомарно.
        self._busy_since: float | None = None
        # nice потока канала index понижен, но в нём только ожидание, копия
        # результата и gc: инференс считают общие для обоих каналов потоки
        # плагина OpenVINO с обычным приоритетом. Нагрузку индексации на CPU
        # ограничивают RAG_OV_NUM_THREADS и index_throttle_seconds.
        initializer = background_pool_initializer if name == "index" else None
        self.executor = concurrent.futures.ThreadPoolExecutor(
            1, thread_name_prefix=f"ov_{name}", initializer=initializer
        )

    @property
    def tok_inf(self) -> ov.InferRequest:
        if self._tok_inf is None:
            self._tok_inf = self.tokenizer.create_infer_request()
        return self._tok_inf

    @property
    def model_inf(self) -> ov.InferRequest:
        if self._model_inf is None:
            self._model_inf = self.model.create_infer_request()
        return self._model_inf

    def _maybe_reset_memory(self):
        self._counter += 1
        if self._counter % self.reset_memory_every_n == 0:
            # release_memory() здесь НЕ вызываем: CompiledModel общая для обоих
            # каналов и для потоков подсчёта токенов, и освобождение её памяти
            # рушит чужие infer-request'ы. Пересоздаём только свои.
            self._tok_inf = self.tokenizer.create_infer_request()
            self._model_inf = self.model.create_infer_request()
            gc.collect()
            # gc только что освободил буферы старых infer-request'ов — момент
            # вернуть их страницы ОС, пока арены glibc их не удержали.
            safe_malloc_trim()

    def infer(self, texts: list[str]) -> np.ndarray:
        """Синхронный инференс. Выполняется в собственном потоке канала."""
        with self._lock:
            # После захвата лока: ожидание в очереди за другим батчем — не зависание.
            self._busy_since = time.monotonic()
            try:
                return self._infer_locked(texts)
            finally:
                self._busy_since = None

    def _infer_locked(self, texts: list[str]) -> np.ndarray:
        self._maybe_reset_memory()

        tok_inf = self.tok_inf
        tok_inf.start_async(inputs=texts)
        tok_inf.wait()
        toks = {
            x.any_name: v.data
            for x, v in zip(self.tokenizer.outputs, tok_inf.output_tensors)
        }

        model_inf = self.model_inf
        model_inf.start_async(inputs=toks)
        model_inf.wait()
        # Копия обязательна: .data — view на буфер infer-request'а, который
        # перепишет следующий батч.
        return np.array(model_inf.output_tensors[0].data, copy=True)

    def busy_seconds(self, now: float) -> float:
        """Сколько идёт текущий вызов; 0 — канал свободен."""
        since = self._busy_since
        return 0.0 if since is None else now - since

    def shutdown(self):
        self.executor.shutdown(wait=False)


class OpenVINOEmbedder:
    def __init__(
        self,
        model_path: str,
        openvino: str = MODEL_IR,
        tokenizer: str = TOKENIZER_IR,
        device: str = "CPU",
    ):
        tokenizer_path = Path(model_path) / tokenizer
        self.core = ov.Core()
        self._path = model_path
        self.model = self.core.compile_model(Path(model_path) / openvino, device, _cfg)
        self.tokenizer = self.core.compile_model(tokenizer_path, device, _cfg)

        self._channels: dict[Role, _InferChannel] = {
            "search": _InferChannel("search", self.model, self.tokenizer),
            "index": _InferChannel("index", self.model, self.tokenizer),
        }
        # Свой infer-request токенизатора на каждый поток: подсчёт длины текста
        # в токенах идёт из потоков парсинга, которых несколько.
        self._tok_local = threading.local()

    def _thread_tokenizer_request(self) -> ov.InferRequest:
        request = getattr(self._tok_local, "request", None)
        if request is None:
            request = self._tok_local.request = self.tokenizer.create_infer_request()
        return request

    def count_tokens(self, text: str) -> int:
        """Длина текста в токенах. Безопасно вызывать из любого потока.

        Не через `CompiledModel.__call__`: он держит один общий InferRequest на
        модель, а сплиттер зовёт подсчёт синхронно из потоков парсинга —
        одновременные вызовы падают с «Infer Request is busy».
        """
        request = self._thread_tokenizer_request()
        request.start_async(inputs=[text])
        request.wait()
        tokens = {
            output.any_name: tensor.data
            for output, tensor in zip(self.tokenizer.outputs, request.output_tensors)
        }
        return len(tokens["input_ids"][0])

    @cached_property
    def prefix(self):
        if "user2" in self._path:
            return {"query": "search_query", "document": "search_document"}
        return None

    def _apply_prefix(self, texts: list[str], task: Literal["query", "document"]):
        # Вход модели — в нижнем регистре: rubert-tiny2 различает регистр, и
        # запрос ЗАГЛАВНЫМИ почти не находит нужный документ. Хранимый текст,
        # BM25 и count_tokens не меняются.
        texts = [x.lower() for x in texts]
        if self.prefix:
            return [f"{self.prefix[task]}{x}" for x in texts]
        return texts

    def encode(self, texts: list[str], task: Literal["query", "document"] = "query"):
        """Синхронный инференс через канал `index`.

        Не напрямую к `CompiledModel` по той же причине, что и в
        `count_tokens`: общий InferRequest не выдерживает вызовов из
        нескольких потоков.
        """
        texts = self._apply_prefix(texts, task)
        result = self._channels["index"].infer(texts)
        embeddings = self.cls_pooling(result)
        embeddings: np.ndarray = self.l2_normalize(embeddings)
        return embeddings

    async def async_encode(
        self,
        texts,
        task: Literal["query", "document"] = "query",
        role: Role = "search",
    ):
        """Инференс в потоке канала своей роли.

        `role="search"` — интерактивные запросы, `role="index"` — фоновая
        индексация (см. `_InferChannel`).
        """
        texts = self._apply_prefix(texts, task)
        channel = self._channels[role]
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(
            channel.executor, partial(channel.infer, texts)
        )
        embeddings = self.cls_pooling(res)
        embeddings: np.ndarray = self.l2_normalize(embeddings)
        return embeddings

    def oldest_infer_age(self) -> float:
        """Сколько идёт самый долгий из текущих вызовов; 0 — все каналы свободны."""
        now = time.monotonic()
        return max(channel.busy_seconds(now) for channel in self._channels.values())

    def inference_alive(self) -> bool:
        """Ни один вызов не висит дольше INFER_STALL_LIMIT_S (для сторожа systemd)."""
        age = self.oldest_infer_age()
        if age < INFER_STALL_LIMIT_S:
            return True
        lg.error(
            "инференс висит %.0f c — сторож systemd не продлевается, "
            "демон будет перезапущен",
            age,
        )
        return False

    def shutdown(self):
        for channel in self._channels.values():
            channel.shutdown()

    @staticmethod
    def cls_pooling(last_hidden_state) -> np.ndarray:
        """CLS-пулинг: берём вектор первого токена ([CLS])."""
        return last_hidden_state[:, 0]

    @staticmethod
    def l2_normalize(embeddings) -> np.ndarray:
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        return embeddings / np.clip(norms, 1e-12, None)
