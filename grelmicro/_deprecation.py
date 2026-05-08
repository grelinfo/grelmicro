"""Internal deprecation helpers for the 0.x to 1.0 transition."""

from __future__ import annotations

import warnings


def warn_legacy(symbol: str, replacement: str) -> None:
    """Emit a DeprecationWarning for a legacy module-level helper.

    `symbol` is the dotted path of the deprecated callable.
    `replacement` is a short snippet pointing at the `Grelmicro` app object
    path that supersedes it.
    """
    warnings.warn(
        f"`{symbol}` is deprecated and will be removed in 1.0.0. "
        f"Use {replacement} instead.",
        DeprecationWarning,
        stacklevel=3,
    )
