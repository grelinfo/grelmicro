from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from grelmicro.sync import Lock
from grelmicro.sync.lock import LockConfig


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MYAPP_",
        env_nested_delimiter="__",
        yaml_file="config.yaml",
    )

    locks: dict[str, LockConfig] = {}

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,  # noqa: ARG003
        file_secret_settings: PydanticBaseSettingsSource,  # noqa: ARG003
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            YamlConfigSettingsSource(settings_cls),
        )


settings = AppSettings()
locks = {
    name: Lock(name, config=config) for name, config in settings.locks.items()
}
