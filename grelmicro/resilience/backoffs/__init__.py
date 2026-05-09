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
from grelmicro.resilience.backoffs.fibonacci import FibonacciBackoffConfig
from grelmicro.resilience.backoffs.linear import LinearBackoffConfig
from grelmicro.resilience.backoffs.random import RandomBackoffConfig

RetryBackoffConfig = Annotated[
    ExponentialBackoffConfig
    | ConstantBackoffConfig
    | LinearBackoffConfig
    | FibonacciBackoffConfig
    | RandomBackoffConfig,
    Discriminator("type"),
]
"""Discriminated union of supported retry backoff configurations."""

__all__ = [
    "ConstantBackoffConfig",
    "ExponentialBackoffConfig",
    "FibonacciBackoffConfig",
    "LinearBackoffConfig",
    "RandomBackoffConfig",
    "RetryBackoffConfig",
]
