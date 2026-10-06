import contextlib
import datetime
import os
import shutil
import sys
from collections.abc import Callable
from functools import cached_property, lru_cache
from pathlib import Path
from random import random
from typing import Any, Literal

import numpy as np
import yaml
from langchain_text_splitters import RecursiveCharacterTextSplitter, TextSplitter
from lsm import LSM
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    JsonConfigSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from src.common.fts import FtsConfig
from src.server.model import EmailConfig
from src.utils import get_logger

if getattr(sys, "frozen", False):
    # если упаковано PyInstaller — путь внутри упакованного приложения
    base_path = Path(sys._MEIPASS)  # type: ignore
else:
    # если запуск из исходников
    base_path = Path(os.path.abspath("."))
    base_path = base_path.relative_to(Path.cwd())


lg = get_logger(__name__)
root = Path(__file__).parent

# Кэш подсчёта длины в токенах. Строки длиннее chunk_size * фактор не
# кэшируются: они уникальны и заняли бы много памяти.
_TOKEN_LEN_CACHE_SIZE = 4096
_TOKEN_LEN_CACHE_CHAR_FACTOR = 8

if os.environ.get("DEV") == "1":
    _root = Path("data/app")
    LANCE_URL = str(_root / "lance")
    LSM_PATH = str(_root / "lsm.ldb")
else:
    _root = Path("~/.files-search").expanduser()
    os.makedirs(_root, exist_ok=True)
    LANCE_URL = str(_root / "lance")
    LSM_PATH = str(_root / "lsm.ldb")

_user_root = Path("~/.ragsearch").expanduser()


def reset_storage() -> None:
    """Удалить индексы и пользовательский конфиг (RESET=1).

    Вызывается явно из точки входа: импорт модуля не должен ничего удалять.
    """
    lg.info("resetting storage: %s, %s", _root, _user_root)
    shutil.rmtree(_root, ignore_errors=True)
    shutil.rmtree(_user_root, ignore_errors=True)
    os.makedirs(_root, exist_ok=True)


def reset_storage_if_requested() -> None:
    if os.environ.get("RESET") == "1":
        reset_storage()


class ExcelParserConfig(BaseModel):
    # Строк данных на лист: дальше лист обрезается с предупреждением, но не теряется.
    max_rows: int = 5000


class OcrConfig(BaseModel):
    """OCR страниц PDF без текстового слоя (src/parsers/ocr.py, src/ocr_worker.py).

    Страницы сверх лимита и не уложившиеся во время не распознаются: одна строка
    WARNING, файл индексируется с тем, что есть. Из окружения секция задаётся
    JSON: RAG_OCR_CFG='{"enabled": false}'.
    """

    enabled: bool = True
    languages: str = "rus+eng"
    # 200, а не 300: на сканах 100 dpi точнее (сумм 94,8% против 92,2%), на настоящих
    # сканах — то же качество, и на 10–20% быстрее (замер КТ-OCR1, 02.10.2026).
    dpi: int = 200
    max_pages_per_file: int = 200
    max_seconds_per_file: float = 1200
    # tessdata_fast (LSTM, int8): Tesseract в MuPDF собран без старого движка.
    tessdata_path: str = str(base_path / "models/tessdata")


class FileScanConfig(BaseModel):
    path: str | None = None
    # Умолчание — домашний каталог, а НЕ пустой список. Пустой список означал,
    # что демон, запущенный без CONFIG_PATH, поднимается, отвечает на /health и
    # /search, но не индексирует ничего — и понять это можно было только по
    # GET /config. Держится это ровно на user_conf.yml: пока он есть, пути
    # берутся оттуда, а стоит его удалить — индексация молча умолкает.
    # Разумное умолчание убирает и переменную окружения из команды запуска.
    #
    # Тесты не задевает: cfg/dev.yml задаёт paths: [] явно, а явное значение
    # перебивает умолчание модели.
    # Умолчание — "/home" (родительский для обеих форм SberOS-путей), чтобы
    # индексация шла с фиксированного каталога независимо от HOME и CONFIG_PATH.
    paths: list[str] = Field(default_factory=lambda: ["/home"])
    max_depth: int | None = None
    # Идти ли по символьным ссылкам при обходе. True: пользователи держат за
    # ссылками рабочие каталоги, и без этого они молча не индексировались.
    # Обход защищён от петель и повторного прохода одного дерева по
    # (st_dev, st_ino) — см.iter_file_metadata.
    follow_symlinks: bool = True
    verbose: bool = False
    filter_files: Callable[[str], bool] | None = None
    filter_meta: Callable[[Any], bool] | None = None  # filter_meta
    filter_dirs: Callable[[str], bool] | None = None
    batch_wait_time: float = 1
    allowed_extensions: list[str] = Field(
        default_factory=lambda: [
            "pdf",
            "docx",
            "doc",
            "pptx",
        ]
    )
    max_size: int = 100 * 1024 * 1024  # максимальный размер файлов для чтения

    model_config = ConfigDict(arbitrary_types_allowed=True)


class EmailScanConfig(BaseModel):
    """Пороги размера для почтового тракта.

    Письмо разбирается только целиком в памяти (MIME-дерево не читается
    потоково), поэтому пороги здесь — про потребление памяти демоном, а не про
    полезность содержимого. Файловый `FileScanConfig.max_size` к почте не
    применяется: письма не проходят через обход файловой системы.

    Порог письма обязан быть выше порога вложения: вложение лежит внутри
    письма в base64 — плюс треть на кодирование и ещё ~2.6% на переносы строк,
    так что вложение в 30 МиБ занимает в письме около 41 МиБ. При меньшем
    пороге письма порог вложения не сработал бы никогда: письмо отсекалось бы
    раньше, чем дело дошло до вложений.

    Пик памяти задаёт именно порог письма: письмо читается и разбирается
    целиком. Поэтому снижать порог вложения имеет смысл только вместе с
    порогом письма — иначе тяжёлое письмо всё равно будет прочитано, а
    вложение отброшено уже после разбора.
    """

    max_message_size: int = 48 * 1024 * 1024
    max_attachment_size: int = 30 * 1024 * 1024


class SplitterCfg(BaseModel):
    chunk_size: int = 500
    chunk_overlap: int = 100


class EmbeddingModelConfig(BaseModel):
    model_type: Literal["jina", "st", "fake", "ov"] = "ov"
    model_path: str = str(base_path / "models/rubert-tiny2_002")
    device: str = "cpu"
    batch_size: int = 8
    passages_promt_name: str | None = None
    query_promt_name: str | None = None


class RarankerModelConfig(BaseModel):
    """Переранжирование гибридной выдачи кросс-энкодером (эксперимент, выключено).

    Модель собирает `make reranker-model` (utils/reranker_model.py) в model_path:
    IR кросс-энкодера и токенизатор пары OpenVINO. alpha — вес реранкера в
    слиянии рангов с гибридом (1 — чистый реранк), src/server/rerank.py.
    """

    enabled: bool = False
    model_path: str = str(base_path / "models/reranker")
    alpha: float = 0.3
    batch_size: int = 8
    device: str = "CPU"


class Config(BaseSettings):
    file_scan_cfg: FileScanConfig = Field(default_factory=FileScanConfig)
    email_scan_cfg: EmailScanConfig = Field(default_factory=EmailScanConfig)
    embed_splitter_cfg: SplitterCfg = Field(default_factory=SplitterCfg)
    embed_model_cfg: EmbeddingModelConfig = Field(default_factory=EmbeddingModelConfig)
    reranker_model_cfg: RarankerModelConfig = Field(default_factory=RarankerModelConfig)
    # Полнотекстовый индекс; индекс с другими параметрами пересобирается при старте.
    fts_cfg: FtsConfig = Field(default_factory=FtsConfig)
    # Сколько документов гибрида переставляет реранкер (если он включён).
    reranker_k: int = 20
    reranker_cutoff: float = 0.1  # не используется; оставлен ради старых конфигов
    ld_db_path: str = LANCE_URL
    lsm_root: str = str(_root)
    lsm_path: str = LSM_PATH
    ld_files_table: str = "files"
    ld_email_table: str = "files_email"
    # Add option for eager initialization (useful for testing PyInstaller builds)
    data_directory: str = str(_user_root)
    # Сколько строк накапливать перед записью в LanceDB. Это НЕ размер батча
    # инференса (тот в embed_model_cfg.batch_size): каждая запись — отдельная
    # транзакция и новая версия таблицы, а компакция версий стоит CPU.
    parquet_record_batch_size: int = 500
    excel_config: ExcelParserConfig = Field(default_factory=ExcelParserConfig)
    ocr_cfg: OcrConfig = Field(default_factory=OcrConfig)
    user_home: str | None = None
    logging_level: Literal[
        "CRITICAL",
        "FATAL",
        "ERROR",
        "WARN",
        "WARNING",
        "INFO",
        "DEBUG",
        "NOTSET",
    ] = "INFO"
    app_logging_level: Literal[
        "CRITICAL",
        "FATAL",
        "ERROR",
        "WARN",
        "WARNING",
        "INFO",
        "DEBUG",
        "NOTSET",
    ] = "INFO"
    # Файл лога (RAG_LOG_FILE). Пустая строка — выключить файловый сток.
    #
    # Зачем он нужен, если есть journald. Демон пишет в stderr, и при исправном
    # journald этого достаточно. Но политика журнала задаётся образом ОС, а не
    # нами: при Storage=none или SplitMode=none пользовательского журнала нет
    # вовсе, а MaxLevelStore=warning выбрасывает всё, что мы пишем на INFO.
    # Со стороны это выглядит одинаково — «сервис работает, логов нет» — и
    # диагностировать нечем. Свой файл не зависит ни от одной из этих настроек.
    #
    # Каталог внутри ~/.files-search выбран сознательно: postrm при purge
    # удаляет его целиком, значит лог с путями к личным файлам пользователя не
    # переживёт удаление пакета (БТ §7 «следов не остаётся»).
    log_file: str | None = str(_root / "logs" / "lib-local-rag.log")
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 3
    asyncio_num_workers: int = 4
    # Пауза перед пересборкой индексов после изменений от watcher'а.
    # Схлопывает всплеск файловых событий в одну перестройку.
    index_refresh_delay: float = 15.0
    # Минимальный интервал между компакциями таблицы: она склеивает фрагменты,
    # но стоит CPU и диска, поэтому не запускается на каждое обновление.
    index_optimize_interval: float = 300.0
    # Интервал (сек) периодического полного прохода — страховка поверх
    # наблюдения за файловой системой. События теряются при переполнении
    # очереди, не приходят для каталогов, ставших читаемыми, и оставляют
    # осиротевшие записи после перемещений; проход заодно зовёт
    # reconcile_deleted и вычищает из индекса пропавшее с диска.
    # Запускается только в простое. 0 — выключить.
    # Задаётся через RAG_FULL_RESCAN_INTERVAL или YAML-конфиг.
    full_rescan_interval: float = 3600.0
    # Пауза (сек) между батчами записи при индексации: отдаёт CPU
    # интерактивной работе пользователя. 0 — индексировать без пауз.
    # Задаётся через RAG_INDEX_THROTTLE_SECONDS или YAML-конфиг.
    index_throttle_seconds: float = 0.0
    # Начинать ли индексацию сразу при старте процесса.
    #
    # True — штатный режим бесшовной поставки: демон поднялся и сам пошёл
    # обходить папки, пользователь ничего не активирует. Управление — у
    # кодингового агента через MCP-мост или HTTP API.
    # False — отладочный режим: демон стартует, но ничего не сканирует, пока
    # не придёт POST /api/v1/activate; поиск по уже собранному индексу
    # продолжает работать.
    #
    # Задаётся через RAG_AUTOSTART_INDEXING или YAML-конфиг.
    autostart_indexing: bool = True
    model_config = SettingsConfigDict(
        extra="ignore",
        cli_parse_args=False,  # os.environ.get("TESTING") != "Y",
        # Без префикса в конфиг попадает любая переменная окружения, совпавшая
        # с именем поля: EMAIL из оболочки роняет старт, USER_HOME и
        # DATA_DIRECTORY подменяют пути.
        env_prefix="RAG_",
    )
    email_poll_interval: int = 60 * 5
    email: EmailConfig | None = None

    @cached_property
    def data_directory_path(self):
        os.makedirs(self.data_directory, exist_ok=True)
        return Path(self.data_directory).expanduser().resolve()

    @cached_property
    def use_hf_tokenizer(self):
        if self.embed_model_cfg.model_type in ["jina", "st", "ov"]:
            return True
        return False

    @cached_property
    def embed_splitter(self) -> TextSplitter:
        if self.embed_model_cfg.model_type == "ov":
            return RecursiveCharacterTextSplitter(
                length_function=self._cached_token_len,
                **self.embed_splitter_cfg.model_dump(),
            )
        if self.use_hf_tokenizer:
            return RecursiveCharacterTextSplitter.from_huggingface_tokenizer(
                self.tokenizer,
                **self.embed_splitter_cfg.model_dump(),
            )
        else:
            return RecursiveCharacterTextSplitter(
                **self.embed_splitter_cfg.model_dump(),
            )

    @cached_property
    def openai(self):
        from openai import AsyncOpenAI  # type: ignore

        return AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    @cached_property
    def gigachat(self):
        from gigachat import GigaChat  # type: ignore

        token = os.getenv("GIGA_TOKEN")
        if not token:
            return None
        ret = GigaChat(credentials=token, verify_ssl_certs=False)
        return ret

    @cached_property
    def gigachat_models(self):
        if self.gigachat:
            return self.gigachat.get_models()
        return None

    def _load_transformers_model(self):
        from transformers import AutoModel  # type: ignore

        model = AutoModel.from_pretrained(
            self.embed_model_cfg.model_path,
            trust_remote_code=True,
            # cache_dir="data/cache",
        ).to(self.embed_model_cfg.device)
        return model

    @cached_property
    def reranker(self):
        """Кросс-энкодер переранжирования или None, если он выключен."""
        cfg = self.reranker_model_cfg
        if not cfg.enabled:
            return None
        from .utils.openvino_reranker import OpenVINOReranker

        lg.info("реранкер: %s", cfg.model_path)
        return OpenVINOReranker(
            cfg.model_path, device=cfg.device.upper(), batch_size=cfg.batch_size
        )

    @cached_property
    def embedder(self):
        if self.embed_model_cfg.model_type == "jina":
            return self._load_transformers_model()
        else:
            raise ValueError(f"{self.embed_model_cfg.model_type=} is not supported")

    @cached_property
    def embed_dim(self):
        if self.embed_model_cfg.model_type == "jina":
            return 1024
        elif self.embed_model_cfg.model_type in {"st", "ov"}:
            if "minilm" in self.embed_model_cfg.model_path.lower():
                return 384
            elif "rubert-tiny" in self.embed_model_cfg.model_path.lower():
                return 312
            elif "user2" in self.embed_model_cfg.model_path.lower():
                return 384
            else:
                return 1024
        elif self.embed_model_cfg.model_type == "fake":
            return 1024
        else:
            raise ValueError(f"{self.embed_model_cfg.model_type=} is not supported")

    def _ov_token_len(self, text: str):
        # Через count_tokens, а не напрямую в CompiledModel: сплиттер вызывается
        # из потоков парсинга, и общий на модель InferRequest их не выдерживает.
        return self.ov_model.count_tokens(text)

    @cached_property
    def _cached_token_len(self) -> Callable[[str], int]:
        """Подсчёт длины с кэшем — то, чем меряет длину сплиттер.

        `RecursiveCharacterTextSplitter` спрашивает длину на каждый кандидат
        разбиения, а не по разу на чанк: сотни вызовов на документ при десятке
        уникальных строк, и каждый гонит инференс токенизатора.

        Кэш возвращает те же значения, поэтому границы чанков не меняются.
        Очень длинные строки не кэшируются: их спрашивают один раз, а держать
        их в памяти дорого.
        """
        cached = lru_cache(maxsize=_TOKEN_LEN_CACHE_SIZE)(self._ov_token_len)
        max_len = self.embed_splitter_cfg.chunk_size * _TOKEN_LEN_CACHE_CHAR_FACTOR

        def token_len(text: str) -> int:
            if len(text) > max_len:
                return self._ov_token_len(text)
            return cached(text)

        return token_len

    def _load_transformers_tokenizer(self):
        from transformers import AutoTokenizer  # type: ignore

        tokenizer = AutoTokenizer.from_pretrained(
            self.embed_model_cfg.model_path,
            trust_remote_code=True,
            cache_dir="data/cache",
        )
        return tokenizer

    def _load_sentense_transformers_model(self):
        from sentence_transformers import SentenceTransformer  # type: ignore

        model_id = self.embed_model_cfg.model_path
        if "rubert-tiny2" in model_id:
            # from sentence_transformers import models

            return SentenceTransformer(
                model_id,
                backend="openvino",
                model_kwargs={
                    "file_name": "openvino/openvino_model_qint8_quantized.xml",
                    "repo_type": "local",
                },
                # local_files_only=True,
            )
            # return model

        return SentenceTransformer(model_id, device=self.embed_model_cfg.device)

    @cached_property
    def tokenizer(self):
        if self.embed_model_cfg.model_type in ["jina", "st"]:
            return self._load_transformers_tokenizer()
        elif self.embed_model_cfg.model_type == "ov":
            return self.ov_model.tokenizer
        elif self.embed_model_cfg.model_type == "st":
            return self.st_model.tokenizer
        else:
            raise ValueError(f"{self.embed_model_cfg.model_type=} is not supported")

    @cached_property
    def st_model(self):
        if self.embed_model_cfg.model_type == "st":
            return self._load_sentense_transformers_model()
        else:
            raise ValueError(
                f"{self.embed_model_cfg.model_type=} is not a valid sentence-transformers model"
            )

    @cached_property
    def ov_model(self):
        lg.debug("loading openvino model")
        from .utils.openvino_encoder import OpenVINOEmbedder

        return OpenVINOEmbedder(self.embed_model_cfg.model_path)

    @cached_property
    def lsm(self):
        return LSM(self.lsm_path)

    @cached_property
    def lance(self):
        """Оба хранилища. Один cached_property на два, чтобы сброс кэша не мог
        обнулить одно и оставить другое."""
        lg.debug("loading lancedb")
        from .utils.lance_wrapper import LanceTables, LanceWrapper, new_lance_session

        # Одна сессия на обе таблицы: бюджет кэшей фиксирован, и сессия на
        # таблицу удвоила бы расход памяти.
        session = new_lance_session()
        return LanceTables(
            files=LanceWrapper(
                ld_db_path=self.ld_db_path,
                embed_dim=self.embed_dim,
                table_name=self.ld_files_table,
                session=session,
                fts=self.fts_cfg,
            ),
            email=LanceWrapper(
                ld_db_path=self.ld_db_path,
                embed_dim=self.embed_dim,
                table_name=self.ld_email_table,
                session=session,
                fts=self.fts_cfg,
            ),
        )

    def embed_passages(self, texts: list[str]) -> list[list[float]] | np.ndarray:
        if self.embed_model_cfg.model_type == "st":
            return self.st_model.encode(
                texts,
                prompt_name=self.embed_model_cfg.passages_promt_name,
                batch_size=self.embed_model_cfg.batch_size,
                show_progress_bar=False,
            ).tolist()
        elif self.embed_model_cfg.model_type == "ov":
            return self.ov_model.encode(texts)
        elif self.embed_model_cfg.model_type == "jina":
            return self.embedder.encode(texts, task="retrieval.passage").tolist()
        elif self.embed_model_cfg.model_type == "fake":
            return [[random() for _ in range(1024)] for _ in texts]
        else:
            raise NotImplementedError()

    def embed_query(self, texts: list[str]) -> list[list[float]] | np.ndarray:
        if self.embed_model_cfg.model_type == "st":
            return self.st_model.encode(
                texts,
                prompt_name=self.embed_model_cfg.query_promt_name,
                batch_size=self.embed_model_cfg.batch_size,
            ).tolist()
        elif self.embed_model_cfg.model_type == "ov":
            return self.ov_model.encode(texts)
        elif self.embed_model_cfg.model_type == "jina":
            return self.embedder.encode(texts, task="retrieval.query").tolist()
        else:
            raise NotImplementedError()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        ret = (
            env_settings,
            DotEnvSettingsSource(
                settings_cls,
                env_file=".env",
            ),
            # init_settings,
        )
        # Get config path from environment or use None as fallback
        conf = os.environ.get("CONFIG_PATH")
        if conf is None:
            # If no config path is found, return basic sources
            return ret

        conf_yaml = conf if conf.endswith((".yml", ".yaml")) else None
        conf_json = conf if conf.endswith(".json") else None

        if conf_yaml:
            ret = (
                env_settings,
                init_settings,  # Use the original init_settings directly
                YamlConfigSettingsSource(
                    settings_cls,
                    yaml_file=conf_yaml,
                ),
                *ret,
            )
        elif conf_json:
            ret = (
                env_settings,
                init_settings,  # Use the original init_settings directly
                JsonConfigSettingsSource(
                    settings_cls,
                    json_file=conf_json,
                ),
                *ret,
            )
        return ret

    @cached_property
    def user_conf_path(self):
        return self.data_directory_path / "user_conf.yml"

    @property
    def user_config(self):
        from .server.model import UserConfig

        p = self.user_conf_path
        if not p.exists():
            return None
        try:
            with open(p) as f:
                return UserConfig.model_validate(yaml.load(f, Loader=yaml.CLoader))
        except Exception:
            # Битый или обрезанный user_conf.yml не должен ронять всё: это
            # свойство читают и API (GET/POST /config), и обход файлов, и
            # почта. Откатываемся к конфигу из YAML/env — демон продолжает
            # работать, а пользователь может перезаписать настройки.
            lg.exception("user_conf.yml повреждён (%s), берём конфиг по умолчанию", p)
            return None

    @property
    def email_config(self):
        if self.user_config and self.user_config.email:
            return self.user_config.email
        else:
            return self.email

    @property
    def email_enabled(self):
        if self.email_config:
            return self.email_config.enabled
        return False

    @property
    def email_root(self) -> Path | None:
        """Каталог с mbox-файлами почты.

        Явный путь из конфига (`email.path_to_email_folder`) приоритетнее
        автопоиска: он позволяет работать без Thunderbird (тесты, macOS) и
        не зависит от структуры профиля.
        """
        from src.thunderbird.find_files import find_email_root

        email_cfg = self.email_config
        if email_cfg and email_cfg.path_to_email_folder:
            p = Path(email_cfg.path_to_email_folder).expanduser()
            if p.is_dir():
                return p
            lg.warning(
                "path_to_email_folder=%s задан, но каталога нет — "
                "почта не индексируется",
                p,
            )
            return None
        return find_email_root(Path(self.user_home) if self.user_home else None)

    @property
    def email_files(self) -> list[tuple[Path, str]]:
        from src.thunderbird.find_files import list_email_folders

        root = self.email_root
        if root is None:
            lg.warning("email root not found — email indexing failed")
            return []
        folders = list_email_folders(root)
        return [(root / prefix, prefix) for prefix in folders]

    @property
    def email_begin_date(self):
        if self.email_config and self.email_config.index_last_n_days:
            return datetime.datetime.now() - datetime.timedelta(
                self.email_config.index_last_n_days
            )
        else:
            return None

    @property
    def user_config_with_default(self):
        from .server.model import UserConfig

        if (cfg := self.user_config) is not None:
            return cfg
        else:
            return UserConfig(
                folders=[str(Path(p).expanduser()) for p in self.file_scan_cfg.paths],
                email=self.email_config or EmailConfig(),
            )

    def update_user_config(self, config: BaseModel):
        """Атомарно записать пользовательский конфиг.

        Открытие целевого файла на "w" усекает его сразу, поэтому обрыв
        процесса посреди yaml.dump оставлял пустой или обрезанный
        user_conf.yml — а его читают и API, и обход файлов, и почта.
        """
        p = self.user_conf_path
        os.makedirs(p.parent, exist_ok=True)
        tmp = p.with_suffix(".yml.tmp")
        try:
            with open(tmp, "w") as f:
                yaml.dump(config.model_dump(), f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
        except OSError:
            with contextlib.suppress(OSError):
                os.remove(tmp)
            raise

    @property
    def scan_paths(self):
        if user_cfg := self.user_config:
            if user_cfg.folders:
                return user_cfg.folders
        return self.file_scan_cfg.paths

    @property
    def scan_max_depth(self) -> int | None:
        """Глубина рекурсивного наблюдения файловых каталогов.

        None — наблюдать всё дерево (recursive=True, по одному inotify-watch на
        каждый подкаталог). Число — ограничить глубину, чтобы не упираться в
        fs.inotify.max_user_watches на большом домашнем каталоге. Совпадает по
        приоритету со scan_paths: пользовательская настройка — главнее конфига.
        """
        if (user_cfg := self.user_config) and user_cfg.max_depth is not None:
            return user_cfg.max_depth
        return self.file_scan_cfg.max_depth
