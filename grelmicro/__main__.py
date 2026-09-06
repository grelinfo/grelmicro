"""The `python -m grelmicro` command line.

One subcommand today, `check`, which renders `Grelmicro.describe()` and turns
its checks into an exit code so CI can run it:

```bash
python -m grelmicro check app:micro
```

Add `--app` to name the web application too. The report then carries the
endpoint table, which says what each registered component does to each route:

```bash
python -m grelmicro check app:micro --app app:app
```

Read more in the [wiring](wiring.md) docs.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from grelmicro._app import Grelmicro

__all__ = ["load_object", "load_target", "main"]

_TARGET_HELP = """\
The app to check, as `module:attribute`, for example `app:micro`. The module
is imported, so it runs whatever it runs at import time.\
"""


_APP_HELP = """\
The web application the app is installed on, as `module:attribute`, for
example `app:app`. Adds the endpoint table, which says what each registered
component does to each route, and checks that `micro.install(app)` was
called.\
"""


class TargetError(Exception):
    """Raised when the `module:attribute` target cannot be resolved."""


def load_object(target: str) -> object:
    """Import `module:attribute` and return whatever it names.

    Raises:
        TargetError: If the target is malformed, the module does not
            import, or the attribute is missing.
    """
    if ":" not in target:
        msg = (
            f"{target!r} is not a module:attribute target. "
            f"Write it as `app:micro`."
        )
        raise TargetError(msg)
    module_name, _, attribute = target.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        msg = f"cannot import module {module_name!r}: {exc}"
        raise TargetError(msg) from exc
    try:
        app = getattr(module, attribute)
    except AttributeError as exc:
        msg = f"module {module_name!r} has no attribute {attribute!r}"
        raise TargetError(msg) from exc
    return app


def load_target(target: str) -> Grelmicro:
    """Import `module:attribute` and return the `Grelmicro` it names.

    Raises:
        TargetError: If the target is malformed, the module does not import,
            the attribute is missing, or it is not a `Grelmicro`.
    """
    from grelmicro._app import Grelmicro  # noqa: PLC0415

    app = load_object(target)
    if not isinstance(app, Grelmicro):
        msg = (
            f"{target!r} is a {type(app).__name__}, not a Grelmicro app. "
            f"Point at the Grelmicro instance."
        )
        raise TargetError(msg)
    return app


def _build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for `python -m grelmicro`."""
    parser = argparse.ArgumentParser(
        prog="python -m grelmicro",
        description="Inspect and check a Grelmicro application.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    check = subcommands.add_parser(
        "check",
        help="Print what the app is wired with and fail on a broken check.",
        description=(
            "Render the app's wiring report. Exits 1 when any check fails, "
            "so it can gate a deployment from CI."
        ),
    )
    check.add_argument("target", help=_TARGET_HELP)
    check.add_argument("--app", help=_APP_HELP, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line and return the process exit code.

    Returns 0 when every check passes, 1 when one fails, and 2 when the
    target cannot be resolved.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    # An app is usually importable only from the directory it lives in, which
    # is how `uvicorn app:app` behaves too.
    if "" not in sys.path:
        sys.path.insert(0, "")
    try:
        micro = load_target(args.target)
        web = None if args.app is None else load_object(args.app)
    except TargetError as exc:
        print(f"error: {exc}", file=sys.stderr)  # noqa: T201
        return 2
    report = micro.describe(web)
    print(report.render())  # noqa: T201
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
