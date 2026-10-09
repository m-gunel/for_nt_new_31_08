import typing

from fastapi import Depends

if typing.TYPE_CHECKING:
    from src.config import Config


_config: "Config" = None  # type: ignore


def get_config(**kwargs):
    from src.config import Config

    global _config
    if _config is None:
        _config = Config(**kwargs)
    return _config


def set_config(cfg: typing.Optional["Config"] = None) -> "Config":
    from src.config import Config

    global _config

    _config = cfg or Config()
    return _config  # type: ignore


def _get_config():
    assert _config is not None
    return _config


class ConfigMixin:
    @property
    def cfg(self):
        return _get_config()


AppConfig: typing.TypeAlias = typing.Annotated["Config", Depends(_get_config)]
