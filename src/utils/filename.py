import re

patt_invalid_chars = re.compile(r'[<>:"/\\|?*\0]')


def clean_filename(filename, replace_with=""):
    # Определяем набор запрещенных символов для кроссплатформенности

    # Заменяем все вхождения запрещенных символов на указанный символ или пустую строку
    clean_name = patt_invalid_chars.sub(replace_with, filename)
    # Дополнительно удаляем ведущие/завершающие пробелы, которые могут быть проблематичными
    return clean_name.strip()
