"""The default table names a SQL provider gives the stores it builds."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, ValidationError

from grelmicro._config import any_case
from grelmicro._unset import DefaultTableName
from grelmicro.errors import SettingsValidationError
from grelmicro.types import TableNames

type _TableNamesField = Annotated[TableNames | None, any_case(TableNames)]


class _TableNamesValue(BaseModel):
    """Validate a `table_names` passed in."""

    model_config = ConfigDict(hide_input_in_errors=True)

    table_names: _TableNamesField = None


def resolve_table_names(table_names: str | None) -> TableNames | None:
    """Validate the `table_names` keyword, matched in any case.

    Returns None when it is None. The environment is never read.

    Raises:
        SettingsValidationError: When the value is not `legacy` or
            `prefixed`. The rejected value is not echoed.
    """
    if table_names is None:
        return None
    try:
        return _TableNamesValue.model_validate(
            {"table_names": table_names}
        ).table_names
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
