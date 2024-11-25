"""Grelmicro Logging Errors."""

from pydantic import ValidationError

from grelmicro.errors import GrelmicroError


class LoggingSettingsError(GrelmicroError):
    """Logging Settings Error."""

    def __init__(self, error: ValidationError) -> None:
        """Initialize the error."""
        details = "\n".join(
            f"- {data['loc'][0]}: {data['msg']} [input={data['input']}]"
            for data in error.errors()
        )

        super().__init__(
            f"Could not parse environment variables for logging settings:\n{details}"
        )
