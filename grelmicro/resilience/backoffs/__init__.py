"""Retry backoff configurations.

Backoff configurations are pure [Pydantic](https://docs.pydantic.dev/)
data classes. A [`Retry`][grelmicro.resilience.Retry] turns a
backoff config into a
[`RetryStrategy`][grelmicro.resilience.RetryStrategy] once per
loop and reads delays from it.
"""

from typing import Annotated

from pydantic import Discriminator

from grelmicro.resilience.backoffs.constant import ConstantBackoff
from grelmicro.resilience.backoffs.exponential import (
    ExponentialBackoff,
)
from grelmicro.resilience.backoffs.fibonacci import FibonacciBackoff
from grelmicro.resilience.backoffs.linear import LinearBackoff
from grelmicro.resilience.backoffs.random import RandomBackoff

RetryBackoffConfig = Annotated[
    ExponentialBackoff
    | ConstantBackoff
    | LinearBackoff
    | FibonacciBackoff
    | RandomBackoff,
    Discriminator("kind"),
]
"""Discriminated union of supported retry backoff configurations."""

__all__ = [
    "ConstantBackoff",
    "ExponentialBackoff",
    "FibonacciBackoff",
    "LinearBackoff",
    "RandomBackoff",
    "RetryBackoffConfig",
]
