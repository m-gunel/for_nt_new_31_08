import re

patt_invalid_chars = re.compile(r'[<>:"/\\|?*\0]')

# NAME_MAX в ext4/tmpfs/xfs — 255 байт, а не символов: буква кириллицы в UTF-8
# занимает два. Четыре байта — под «.eml», который get_file_path дописывает к
# очищенной теме письма.
MAX_NAME_BYTES = 255 - len(".eml")
# Хвост после точки длиннее — уже часть имени («Отчёт v1.2 итог»), а не
# расширение, которое нужно сохранить при обрезке.
_MAX_EXT_CHARS = 10


def _utf8_size(text: str) -> int:
    # surrogatepass: строка может нести суррогаты (surrogateescape от сырых
    # байтов), строгое кодирование на них падает, а здесь нужен только размер.
    return len(text.encode("utf-8", "surrogatepass"))


def _fit_utf8(text: str, limit: int) -> str:
    """Самое длинное начало text не длиннее limit байт, без обломков символов."""
    size = 0
    for i, ch in enumerate(text):
        size += _utf8_size(ch)
        if size > limit:
            return text[:i]
    return text


def clean_filename(filename, replace_with="", fallback="file"):
    """Имя для файла во временном каталоге: без запрещённых символов, не длиннее
    MAX_NAME_BYTES байт в UTF-8; пустое, «.» и «..» заменяются на fallback."""
    clean_name = patt_invalid_chars.sub(replace_with, filename).strip()
    if _utf8_size(clean_name) > MAX_NAME_BYTES:
        # Расширение сохраняем: по нему вложение открывается нужной программой.
        stem, dot, ext = clean_name.rpartition(".")
        if not (stem and 0 < len(ext) <= _MAX_EXT_CHARS and ext.isalnum()):
            stem, dot, ext = clean_name, "", ""
        tail = dot + ext
        clean_name = _fit_utf8(stem, MAX_NAME_BYTES - _utf8_size(tail)) + tail
    # Пустое имя, «.» и «..» указывают на сам каталог или его родителя.
    if clean_name in ("", ".", ".."):
        return fallback
    return clean_name
