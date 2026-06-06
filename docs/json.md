# JSON

Fast JSON serialization and deserialization. Use it whenever you encode or decode JSON on a hot path.

- **Fast**: prefers [orjson](https://github.com/ijl/orjson) when installed, about 7 times faster than stdlib `json`.
- **Zero-config**: falls back to the standard library `json` module transparently.
- **No per-call overhead**: the backend is selected once at import time.

## Quick start

```python
--8<-- "json/basic.py"
```

`orjson` is included in the `standard` extra. Without it, the module falls back to stdlib `json`:

```bash
pip install grelmicro[standard]
```

## Datetime handling

`datetime` objects are automatically serialized to ISO 8601 strings. Deserialization returns a string, not a `datetime` object:

```python
--8<-- "json/datetime_handling.py"
```

## Supported types

`json_dumps_bytes` and `json_dumps_str` serialize the same set of types on both backends:

- `str`, `int`, `float`, `bool`, `None`
- `dict` (and any `Mapping`) with string keys
- `list` and `tuple` (a `tuple` serializes to a JSON array)
- `datetime`, serialized to an ISO 8601 string

`json_loads` accepts `bytes` or `str` and returns the matching Python value: `dict`, `list`, `str`, `int`, `float`, `bool`, or `None`. JSON arrays always decode to `list`, so a `tuple` does not round-trip back to a `tuple`.

### Serializer boundaries

These functions cover JSON-native values only. Unsupported types raise `TypeError`. This includes `set`, `frozenset`, `bytes`, `Decimal`, and arbitrary objects without a JSON-encodable form:

```python
--8<-- "json/serializer_boundaries.py"
```

This is the boundary against the cache serializers. The low-level `json` functions never run user code on a value: a non-encodable type raises rather than guessing. When you need richer types or model round-trips, use the cache serializers instead. `PydanticSerializer` handles validated models, and `PickleSerializer` handles arbitrary picklable objects on trusted backends. See the [cache serialization docs](cache.md#serialization) for the trade-offs.

??? note "Cache integration"
    For caching, use the built-in serializer classes instead of the low-level functions. See the [cache serialization docs](cache.md#serialization) for `JsonSerializer`, `PydanticSerializer`, and `PickleSerializer`.

??? note "Fallback behavior"
    The module prefers [orjson](https://github.com/ijl/orjson) and falls back to the standard library `json` module when orjson is not installed. The choice happens once at import time, so there is no per-call branching.

    The fallback triggers only on a missing orjson import, not at runtime per value. Install the `standard` extra to get orjson:

    ```bash
    pip install grelmicro[standard]
    ```

    Both backends produce compact output with no extra whitespace and apply the same `datetime` handling. Call `has_orjson()` to check which backend is active:

    ```python
    from grelmicro.json import has_orjson

    if has_orjson():
        print("Using orjson")
    else:
        print("Using stdlib json")
    ```

## Performance

`orjson` is about 7 times faster than stdlib `json` for serialization. The module chooses the implementation at import time, so there is no per-call branching.

| Method | Speed |
|--------|-------|
| `orjson` (with `grelmicro[standard]`) | ~0.2 us/call |
| stdlib `json` (fallback) | ~1.5 us/call |

## API Reference

::: grelmicro.json
    options:
      show_root_heading: false
      members_order: source
