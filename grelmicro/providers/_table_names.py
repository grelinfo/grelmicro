"""The default table names a SQL provider gives the stores it builds."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from grelmicro._config import any_case
from grelmicro._unset import DefaultTableName
from grelmicro.errors import SettingsValidationError

type TableNames = Literal["legacy", "prefixed"]
"""The default table names a SQL provider gives its stores, in any case.

`legacy` keeps the names stores use until 0.46.0, such as `locks`.
`prefixed` gives each the `grelmicro_` prefix, such as `grelmicro_locks`,
the default from 0.46.0.
"""

type _TableNamesField = Annotated[TableNames | None, any_case(TableNames)]


class _TableNamesValue(BaseModel):
    """Validate a `table_names` passed in."""

    model_config = ConfigDict(hide_input_in_errors=True)

    table_names: _TableNamesField = None


class _TableNamesEnvSettings(BaseSettings):
    """Read only `{env_prefix}TABLE_NAMES` from the environment."""

    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)

    table_names: _TableNamesField = None


def resolve_table_names(
    table_names: str | None, *, env_prefix: str, env_load: bool
) -> TableNames | None:
    """Resolve `table_names` from the keyword or `{env_prefix}TABLE_NAMES`.

    The keyword wins. When it is None and `env_load` is True, the
    environment is read. Returns None when neither is set.

    Raises:
        SettingsValidationError: When the value is not `legacy` or
            `prefixed`. The rejected value is not echoed.
    """
    try:
        if table_names is not None:
            return _TableNamesValue.model_validate(
                {"table_names": table_names}
            ).table_names
        if not env_load:
            return None
        return _TableNamesEnvSettings(_env_prefix=env_prefix).table_names
    except ValidationError as error:
        raise SettingsValidationError(error) from None


def resolve_table_name(table_name: str, table_names: TableNames | None) -> str:
    """Return the table name a store uses, given its provider's `table_names`.

    A name passed in is kept. A default follows `table_names`, and stays a
    default that warns when `table_names` is None.
    """
    if not isinstance(table_name, DefaultTableName) or table_names is None:
        return table_name
    if table_names == "legacy":
        return str(table_name)
    return table_name.becomes
