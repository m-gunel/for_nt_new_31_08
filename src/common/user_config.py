"""Пользовательские настройки: user_conf.yml и тело POST /api/v1/config.

Отдельно от src/server/model.py: их читает src/config.py, а импорт пакета
src.server поднимает всё приложение — FastAPI, индексатор и парсеры.
"""

from pydantic import BaseModel, Field


class SberchatConfig(BaseModel):
    enabled: bool = False


class UserConfig(BaseModel):
    folders: list[str] | None = Field(
        None, description="Список директорий для сканирвоания"
    )
    # None — всё дерево: inotify-watch на каждый подкаталог, на большом домашнем
    # каталоге упирается в fs.inotify.max_user_watches. Число — только каталоги
    # до этой вложенности включительно, без рекурсии watchdog'а.
    max_depth: int | None = Field(
        None, description="Глубина рекурсивного наблюдения файлового вотчера"
    )
    sberchat: SberchatConfig = Field(default_factory=SberchatConfig)
