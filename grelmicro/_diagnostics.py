"""Stable codes for grelmicro's startup diagnostics.

Every report grelmicro emits at startup carries a code, so it is greppable in
a log, linkable to a page that explains it, and assertable in a test without
pinning the wording.

The code is a kebab-case slug rather than a number. A number would say nothing
to the reader, and grelmicro has few enough diagnostics that names never run
out. It matches what `pydantic` does for the same reason.

The code carries no severity letter. The same problem is a warning in one
deployment and an error in another: an unmet backend scope is reported when no
tier is declared and refused in `staging` and `production`. Python's own
`-W error` promotes any warning to an exception too, so a letter baked into
the code would go stale on someone else's command line. Severity belongs to
the report, not to the identity of the problem.

Two kinds of report exist and they are silenced differently, so this module
does not pretend there is one mechanism:

- A **warning** carries a category of its own and is filtered by that
  category, never by matching the message text:

  ```toml
  filterwarnings = ["ignore::grelmicro.BackendScopeWarning"]
  ```

- An **error** is raised and cannot be filtered. It is averted by fixing the
  configuration it names, which each diagnostic page spells out.

The user-facing reference is `docs/diagnostics.md`, one section per code.
"""

from __future__ import annotations

from typing import Final

DOCS_URL: Final = "https://grelmicro.grel.info/diagnostics/#"
"""Base URL for the diagnostics reference, one anchor per code.

An anchor is usually the fragile choice, because a renamed heading breaks
every link already sitting in a log. Here the heading *is* the code, so the
anchor is exactly as stable as the identifier: renaming one means renaming the
other, which is a breaking change either way. `mkdocs build --strict` fails on
a heading that stops matching a code.
"""

ENV_LOAD_OFF: Final = "env-load-off"
"""A `GREL_*` variable is set but `GREL_ENV_LOAD` is off, so it was not applied."""

UNKNOWN_ENVIRONMENT: Final = "unknown-environment"
"""`GREL_ENVIRONMENT` names no known tier, so the backend check runs undeclared."""

BACKEND_SCOPE: Final = "backend-scope"
"""A bound backend reaches less far than its component requires."""

AMBIENT_BINDING: Final = "ambient-binding"
"""Ambient components are registered but the binding middleware is missing."""

PROVIDER_ORDER: Final = "provider-order"
"""A Provider is listed after the Component that borrows it."""

SENTINEL_PASSWORD: Final = "sentinel-password"  # noqa: S105
"""A Sentinel password is set but the URL scheme cannot apply it.

The name reads as a credential to a secret scanner. It is a diagnostic code.
"""


def diagnostic(code: str, message: str) -> str:
    """Return `message` followed by its code and the page that explains it.

    The code trails the sentence rather than leading it, so the reader meets
    the problem before the label, the way `mypy` and `ruff` render theirs.
    """
    return f"{message} [{code}] {DOCS_URL}{code}"
