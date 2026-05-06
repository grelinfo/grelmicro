"""Retry backoff configurations.

Backoff configurations are pure [Pydantic](https://docs.pydantic.dev/)
data classes. A [`Retry`][grelmicro.resilience.Retry] turns a
backoff config into a
[`RetryStrategy`][grelmicro.resilience.RetryStrategy] once per
loop and reads delays from it.
"""

from typing import Annotated

from pydantic import Discriminator

from grelmicro.resilience.backoffs.constant import ConstantBackoffConfig
from grelmicro.resilience.backoffs.exponential import (
    ExponentialBackoffConfig,
)

RetryBackoffConfig = Annotated[
    ExponentialBackoffConfig | ConstantBackoffConfig,
    Discriminator("type"),
]
"""Discriminated union of supported retry backoff configurations."""

__all__ = [
    "ConstantBackoffConfig",
    "ExponentialBackoffConfig",
    "RetryBackoffConfig",
]
