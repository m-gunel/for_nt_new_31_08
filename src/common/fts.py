from pydantic import BaseModel, ConfigDict, Field


class FtsConfig(BaseModel):
    """Параметры полнотекстового индекса LanceDB — единственное место умолчаний.

    Раньше индекс строился пресетом `ru_stem`. Пресет не донастраивается: при
    заданном имени lancedb игнорирует остальные параметры, а в нём
    max_token_length=40 — в байтах UTF-8. Русские слова от 20 букв
    («выгодоприобретателей») выпадали и из индекса, и из запроса. Поэтому
    параметры заданы явно и все: у `lancedb.index.FTS` свои умолчания
    (английский, стоп-слова и ascii_folding включены), нам они не подходят.

    Регистр и ё/е склеивает русский стеммер (Счёт = СЧЕТА = счет). При
    stem=False или другом языке ё и е различаются.

    Индекс со старыми параметрами пересобирается при старте демона
    (`Index.migrate_fts_config`): сверка идёт по `index_details` индекса.
    """

    model_config = ConfigDict(extra="forbid")

    base_tokenizer: str = "simple"
    language: str = "Russian"
    stem: bool = True
    lower_case: bool = True
    # В байтах UTF-8, кириллица — два байта на букву. Самые длинные осмысленные
    # токены на банковском корпусе — 58 байт, hex-хэши — 76: запас есть, а
    # защита от мусорных сверхдлинных токенов остаётся.
    max_token_length: int | None = Field(100, ge=1)
    # Как в пресете ru_stem. Стоп-слова Lance отсекает уже после стемминга:
    # «это», «или» проходят как «эт», «ил», так что фильтр по-русски не работает.
    remove_stop_words: bool = False
    ascii_folding: bool = False
    with_position: bool = False
