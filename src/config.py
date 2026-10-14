import contextlib
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
from src.utils import get_logger

if getattr(sys, "frozen", False):
    # Бандл PyInstaller: ресурсы распакованы в sys._MEIPASS.
    base_path = Path(sys._MEIPASS)  # type: ignore
else:
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
    # Свой user_conf.yml: иначе dev-демон и RESET=1 меняют конфиг продукта.
    _user_root = _root / "ragsearch"
    LANCE_URL = str(_root / "lance")
    LSM_PATH = str(_root / "lsm.ldb")
else:
    _root = Path("~/.files-search").expanduser()
    _user_root = Path("~/.ragsearch").expanduser()
    os.makedirs(_root, mode=0o700, exist_ok=True)
    LANCE_URL = str(_root / "lance")
    LSM_PATH = str(_root / "lsm.ldb")


# Носители токенов агентов в lsm_root: хэши читает демон (src/server/auth.py),
# сами токены — мосты; пишет их scripts/register_mcp.py.
AUTH_FILE_NAME = "auth.json"
TOKENS_DIR_NAME = "tokens"
# Хэши отозванных токенов (scripts/register_mcp.py): без них регистрация
# вернула бы отозванный токен из копии в другом доме.
WITHDRAWN_FILE_NAME = "withdrawn_tokens"
# Это не индекс, а доступ мостов к API: снеси их RESET=1 — и всё 401 до
# следующей регистрации агентов.
_KEPT_ON_RESET = frozenset({AUTH_FILE_NAME, TOKENS_DIR_NAME, WITHDRAWN_FILE_NAME})


def _is_home_or_ancestor(path: Path) -> bool:
    """Путь — домашний каталог или его предок: такое не удаляем и не закрываем."""
    home = Path(os.path.expanduser("~")).resolve()
    path = path.expanduser().resolve()
    return path == home or path in home.parents


def _may_remove(target: Path) -> bool:
    if _is_home_or_ancestor(target):
        lg.warning("RESET=1: %s — домашний каталог или его предок, не удаляю", target)
        return False
    return True


def _clear_keeping_auth(root: Path) -> None:
    """Удалить содержимое root, кроме носителей токенов агентов."""
    if not root.is_dir():
        return
    for entry in root.iterdir():
        if entry.name in _KEPT_ON_RESET:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)


def reset_storage(cfg: "Config") -> None:
    """Удалить индексы и пользовательский конфиг (RESET=1) по путям из конфига.

    Вызывается из точки входа после настройки логирования: импорт модуля не
    должен ничего удалять. user_conf.yml удаляется файлом, как в
    POST /api/v1/reset: data_directory задаётся снаружи и может быть общим.
    """
    lsm_root = Path(cfg.lsm_root).expanduser()
    lance = Path(cfg.ld_db_path).expanduser()
    lg.info(
        "RESET=1: удаляю %s (кроме %s), %s и %s",
        lsm_root,
        ", ".join(sorted(_KEPT_ON_RESET)),
        lance,
        cfg.user_conf_path,
    )
    if _may_remove(lsm_root):
        _clear_keeping_auth(lsm_root)
    if _may_remove(lance):
        shutil.rmtree(lance, ignore_errors=True)
    cfg.user_conf_path.unlink(missing_ok=True)
    os.makedirs(lsm_root, mode=0o700, exist_ok=True)


def reset_storage_if_requested(cfg: "Config") -> None:
    if os.environ.get("RESET") == "1":
        reset_storage(cfg)


def protect_storage(cfg: "Config") -> None:
    """Закрыть каталоги данных от других пользователей (0700).

    В индексе, очередях и кэше OCR — полный текст документов, в
    user_conf.yml — список папок. chmod на каждом старте закрывает и каталоги,
    созданные раньше по umask. Отказ не роняет демон.
    """
    for raw in (cfg.lsm_root, cfg.data_directory):
        path = Path(raw).expanduser()
        if _is_home_or_ancestor(path):
            continue
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(path, 0o700)
        except OSError as exc:
            lg.warning("не удалось закрыть каталог %s правами 0700: %s", path, exc)


def _ov_output_dim(embedder: Any) -> int | None:
    """Длина вектора по статической форме выхода OpenVINO-модели.

    None — не узнать: модель подменена (в тестах, без CompiledModel) или
    последняя ось выхода динамическая; тогда embed_dim судит по имени каталога.
    """
    compiled = getattr(embedder, "model", None)
    if compiled is None:
        return None
    shape = compiled.outputs[0].get_partial_shape()
    if shape.rank.is_dynamic:
        return None
    last = shape[shape.rank.get_length() - 1]
    return last.get_length() if last.is_static else None


class ExcelParserConfig(BaseModel):
    # Строк листа сверху, считая пустые (плюс SCAN_ROWS на шапку, src/parsers/xlsx.py):
    # дальше лист обрезается с предупреждением, но не теряется.
    max_rows: int = 5000


class OcrConfig(BaseModel):
    """OCR страниц PDF без текстового слоя (src/parsers/ocr.py, src/ocr_worker.py).

    Страницы сверх лимита и не уложившиеся во время не распознаются: одна строка
    WARNING, файл индексируется с тем, что есть. Из окружения секция задаётся
    JSON: RAG_OCR_CFG='{"enabled": false}'.
    """

    enabled: bool = True
    languages: str = "rus+eng"
    # 200, а не 300: качество на сканах не хуже, а распознавание быстрее.
    dpi: int = 200
    max_pages_per_file: int = 200
    max_seconds_per_file: float = 1200
    # tessdata_fast (LSTM, int8): Tesseract в MuPDF собран без старого движка.
    tessdata_path: str = str(base_path / "models/tessdata")


class FileScanConfig(BaseModel):
    path: str | None = None
    # "/home", а не домашний каталог: общий корень обеих форм путей SberOS, не
    # зависит от HOME и CONFIG_PATH. Обходятся дома всех пользователей, открытые
    # правами uid демона, и без max_depth под inotify встаёт каждый подкаталог
    # (fs.inotify.max_user_watches). Не пустой список: с ним демон работает, но
    # не индексирует ничего. Непустой folders из user_conf.yml главнее;
    # cfg/dev.yml для тестов задаёт paths: [] явно.
    paths: list[str] = Field(default_factory=lambda: ["/home"])
    max_depth: int | None = None
    # True: рабочие каталоги часто лежат за симлинками. От петель и повторного
    # обхода дерева защищает учёт (st_dev, st_ino) в iter_file_metadata.
    follow_symlinks: bool = True
    verbose: bool = False
    filter_files: Callable[[str], bool] | None = None
    filter_meta: Callable[[Any], bool] | None = None
    filter_dirs: Callable[[str], bool] | None = None
    batch_wait_time: float = 1
    # Не используется: форматы задаёт реестр парсеров (src/parsers).
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


class SplitterCfg(BaseModel):
    chunk_size: int = 500
    chunk_overlap: int = 100


class EmbeddingModelConfig(BaseModel):
    # Эмбеддинги считает OpenVINO (Config.ov_model) по model_path. model_type
    # меняет только нарезку (jina/st — токенизатор transformers) и embed_dim;
    # device и *_promt_name никто не читает.
    model_type: Literal["jina", "st", "fake", "ov"] = "ov"
    model_path: str = str(base_path / "models/rubert-tiny2_002")
    device: str = "cpu"
    # Не увеличивать: с большим батчем индексация зависает (CPU 0 %).
    batch_size: int = 8
    passages_promt_name: str | None = None
    query_promt_name: str | None = None


class RarankerModelConfig(BaseModel):
    """Переранжирование гибридной выдачи кросс-энкодером (src/server/rerank.py).

    Модель — BAAI/bge-reranker-v2-m3 (Apache-2.0), OpenVINO IR с весами INT8 в
    models/reranker: лежит в проекте, как модель эмбеддингов; файл весов разбит на
    части меньше 100 МБ (лимит GitHub), их склеивает сборка (main.spec) или сам
    реранкер при запуске из исходников. Пересобрать — `make reranker-model`.
    alpha — вес реранкера в слиянии рангов с гибридом (1 — чистый реранк), keep_top —
    сколько первых документов гибрида не переставлять. Умолчания — по замеру на
    наборе hr (контрольная часть, 170 запросов): Hit@1 0,441 → 0,788, nDCG@10
    0,606 → 0,728; цена — ~12 с на поиск на тестовой машине при половине ядер и
    ~0,8 ГБ памяти.
    """

    enabled: bool = True
    model_path: str = str(base_path / "models/reranker")
    alpha: float = 1.0
    keep_top: int = 0
    batch_size: int = 8
    device: str = "CPU"


class Config(BaseSettings):
    file_scan_cfg: FileScanConfig = Field(default_factory=FileScanConfig)
    embed_splitter_cfg: SplitterCfg = Field(default_factory=SplitterCfg)
    embed_model_cfg: EmbeddingModelConfig = Field(default_factory=EmbeddingModelConfig)
    reranker_model_cfg: RarankerModelConfig = Field(default_factory=RarankerModelConfig)
    # Полнотекстовый индекс; индекс с другими параметрами пересобирается при старте.
    fts_cfg: FtsConfig = Field(default_factory=FtsConfig)
    # Сколько документов гибрида переставляет реранкер (если он включён): на
    # наборе hr 30 кандидатов дали ту же первую десятку, что 20, на 0,5 с дольше.
    reranker_k: int = 20
    reranker_cutoff: float = 0.1  # не используется; оставлен ради старых конфигов
    ld_db_path: str = LANCE_URL
    lsm_root: str = str(_root)
    lsm_path: str = LSM_PATH
    ld_files_table: str = "files"
    # Каталог user_conf.yml (настройки из POST /config); «~» раскрывается в
    # data_directory_path.
    data_directory: str = str(_user_root)
    # Сколько строк накапливать перед записью в LanceDB. Это НЕ размер батча
    # инференса (тот в embed_model_cfg.batch_size): каждая запись — отдельная
    # транзакция и новая версия таблицы, а компакция версий стоит CPU.
    parquet_record_batch_size: int = 500
    excel_config: ExcelParserConfig = Field(default_factory=ExcelParserConfig)
    ocr_cfg: OcrConfig = Field(default_factory=OcrConfig)
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
    # Файл лога; пустая строка — только stderr. Нужен, когда образ ОС не хранит
    # наш журнал: Storage=none, SplitMode=none или MaxLevelStore=warning.
    # Внутри ~/.files-search, чтобы postrm --purge удалил лог с путями к личным
    # файлам вместе с индексом (БТ §7).
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
    # Интервал (с) полного прохода в простое; 0 — выключить. Страховка поверх
    # наблюдения ФС: события теряются при переполнении очереди и не приходят для
    # каталогов, ставших читаемыми. Проход же через reconcile_deleted вычищает
    # из индекса пропавшее с диска.
    full_rescan_interval: float = 3600.0
    # Пауза (с) между батчами записи — отдаёт CPU пользователю; 0 — без пауз.
    index_throttle_seconds: float = 0.0
    # True — штатно: индексация стартует вместе с демоном. False — для отладки:
    # обход ждёт POST /api/v1/activate, поиск по готовому индексу работает.
    autostart_indexing: bool = True
    model_config = SettingsConfigDict(
        extra="ignore",
        cli_parse_args=False,
        # Без префикса в конфиг попадает любая переменная окружения, совпавшая
        # с именем поля: DATA_DIRECTORY из оболочки подменил бы путь.
        env_prefix="RAG_",
    )

    @cached_property
    def data_directory_path(self):
        # Создаётся раскрытый путь: makedirs("~/...") завёл бы в рабочем
        # каталоге каталог с буквальным именем «~».
        path = Path(self.data_directory).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

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
    def embed_dim(self):
        if self.embed_model_cfg.model_type == "ov":
            # Под embed_dim создаётся таблица LanceDB: размерность, угаданная по
            # имени каталога, разошлась бы с векторами на первой же записи.
            if (dim := _ov_output_dim(self.ov_model)) is not None:
                return dim
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
        # Только через count_tokens (свой InferRequest на поток), не через общий
        # self.ov_model.tokenizer([text]): сплиттер зовут из потоков парсинга, и
        # на общем InferRequest они ловят «Infer Request is busy».
        return self.ov_model.count_tokens(text)

    @cached_property
    def _cached_token_len(self) -> Callable[[str], int]:
        """Длина в токенах для сплиттера, с кэшем.

        Сплиттер спрашивает длину на каждый кандидат разбиения — сотни прогонов
        токенизатора на документ при десятке уникальных строк. Границы чанков
        кэш не меняет; очень длинные строки не кэшируются.
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

    @cached_property
    def tokenizer(self):
        if self.embed_model_cfg.model_type in ["jina", "st"]:
            return self._load_transformers_tokenizer()
        elif self.embed_model_cfg.model_type == "ov":
            return self.ov_model.tokenizer
        else:
            raise ValueError(f"{self.embed_model_cfg.model_type=} is not supported")

    @cached_property
    def ov_model(self):
        lg.debug("loading openvino model")
        from .utils.openvino_encoder import OpenVINOEmbedder

        return OpenVINOEmbedder(self.embed_model_cfg.model_path)

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
    def lsm(self):
        return LSM(self.lsm_path)

    @cached_property
    def lance(self):
        """Таблицы индекса (см. LanceTables)."""
        lg.debug("loading lancedb")
        from .utils.lance_wrapper import LanceTables, LanceWrapper, new_lance_session

        return LanceTables(
            files=LanceWrapper(
                ld_db_path=self.ld_db_path,
                embed_dim=self.embed_dim,
                table_name=self.ld_files_table,
                session=new_lance_session(),
                fts=self.fts_cfg,
            ),
        )

    def embed_passages(self, texts: list[str]) -> list[list[float]] | np.ndarray:
        if self.embed_model_cfg.model_type == "ov":
            return self.ov_model.encode(texts)
        elif self.embed_model_cfg.model_type == "fake":
            return [[random() for _ in range(1024)] for _ in texts]
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
        )
        conf = os.environ.get("CONFIG_PATH")
        if conf is None:
            # init_settings тут нет: без CONFIG_PATH аргументы Config(...) игнорируются.
            return ret

        conf_yaml = conf if conf.endswith((".yml", ".yaml")) else None
        conf_json = conf if conf.endswith(".json") else None

        if conf_yaml:
            ret = (
                env_settings,
                init_settings,
                YamlConfigSettingsSource(
                    settings_cls,
                    yaml_file=conf_yaml,
                ),
                *ret,
            )
        elif conf_json:
            ret = (
                env_settings,
                init_settings,
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
        from .common.user_config import UserConfig

        p = self.user_conf_path
        if not p.exists():
            return None
        try:
            with open(p) as f:
                return UserConfig.model_validate(yaml.load(f, Loader=yaml.CLoader))
        except Exception:
            # Битый user_conf.yml не должен ронять API и обход файлов:
            # откатываемся к конфигу из YAML/env, настройки можно перезаписать.
            lg.exception("user_conf.yml повреждён (%s), берём конфиг по умолчанию", p)
            return None

    @property
    def user_config_with_default(self):
        from .common.user_config import UserConfig

        default_folders = [str(Path(p).expanduser()) for p in self.file_scan_cfg.paths]
        if (cfg := self.user_config) is not None:
            if not cfg.folders:
                # Как в scan_paths: пустой или null список — пути из YAML. GET
                # /config и мост (песочница чтения, local_add_path) должны видеть
                # те же корни, что обходит демон.
                return cfg.model_copy(update={"folders": default_folders})
            return cfg
        else:
            return UserConfig(folders=default_folders)

    def update_user_config(self, config: BaseModel):
        """Атомарно записать user_conf.yml (временный файл + os.replace).

        Прямая запись с "w" сразу усекает файл, и обрыв посреди yaml.dump
        оставил бы его пустым или обрезанным.
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
        """Глубина наблюдения каталогов (см. UserConfig.max_depth).

        Как и в scan_paths, пользовательская настройка главнее конфига.
        """
        if (user_cfg := self.user_config) and user_cfg.max_depth is not None:
            return user_cfg.max_depth
        return self.file_scan_cfg.max_depth
