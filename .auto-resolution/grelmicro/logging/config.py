"""Logging Configuration."""

from enum import StrEnum
from typing import Self

from pydantic import Field
from pydantic_settings import BaseSettings


class _CaseInsensitiveEnum(StrEnum):
    @classmethod
    def _missing_(cls, value: object) -> Self | None:
        value = str(value).lower()
        for member in cls:
            if member.lower() == value:
                return member
        return None


class LoggingLevelType(_CaseInsensitiveEnum):
    """Logging Level Enum."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class LoggingFormatType(_CaseInsensitiveEnum):
    """Logging Format Enum."""

    JSON = "JSON"
    TEXT = "TEXT"


class LoggingSettings(BaseSettings):
    """Logging Settings."""

    LOG_LEVEL: LoggingLevelType = LoggingLevelType.INFO
    LOG_FORMAT: LoggingFormatType | str = Field(
        LoggingFormatType.JSON, union_mode="left_to_right"
    )
