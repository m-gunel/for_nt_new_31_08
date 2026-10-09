"""Пользовательские настройки: user_conf.yml и тело POST /api/v1/config.

Отдельно от src/server/model.py: их читает src/config.py, а импорт пакета
src.server поднимает всё приложение — FastAPI, индексатор и парсеры.
"""

from pydantic import BaseModel, Field, field_validator

# Окно почты по умолчанию, дней.
EMAIL_WINDOW_DAYS = 30


class EmailConfig(BaseModel):
    enabled: bool = False
    path_to_email_folder: str = ""
    # Окно по умолчанию, а не None: None — это «весь ящик за все годы», то
    # есть на порядок больше чанков, чем весь файловый корпус. Совпадает с
    # cfg/example.yml; явный null в запросе снимает ограничение.
    index_last_n_days: int | None = EMAIL_WINDOW_DAYS
    offsets: dict[str, int] | None = None

    @field_validator("index_last_n_days")
    @classmethod
    def _window_from_one_day(cls, value: int | None) -> int | None:
        """0 и отрицательное из user_conf.yml или YAML — окно по умолчанию.

        Окна короче дня не бывает. POST /config такие значения отвергает
        (EmailConfigUpdate), а при чтении отказ обнулил бы весь user_conf.yml
        вместе со списком папок.
        """
        if value is not None and value < 1:
            return EMAIL_WINDOW_DAYS
        return value


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
    email: EmailConfig = Field(default_factory=EmailConfig)
    sberchat: SberchatConfig = Field(default_factory=SberchatConfig)
