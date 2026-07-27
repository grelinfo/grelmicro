# Type-checking tests

These files assert the *public* API types are what users get. They are never
executed: `assert_type` is a static claim, checked by `ty` and by `mypy`.

They exist because grelmicro ships `py.typed`, so every annotation here is part
of the contract. A decorator that quietly widens a return type to `Any`, or a
factory that loses its `Self`, breaks downstream code without failing a single
runtime test.

Files are deliberately **not** named `test_*.py`, so pytest does not collect
them. Coverage is scoped to `grelmicro`, so they do not affect the coverage
gate either.

To check them:

```sh
uv run ty check
uv run mypy
```

Both run in `pre-commit`, so a regression fails before it lands.

When adding a primitive to the public API, add its type assertions here.
