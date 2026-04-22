# JSON

The `json` module provides fast JSON serialization and deserialization using [orjson](https://github.com/ijl/orjson) when available, with automatic fallback to the standard library `json` module.

The JSON backend is selected once at import time, so there is no per-call overhead.

## Installation

`orjson` is included in the `standard` extra:

```bash
pip install grelmicro[standard]
```

Without the extra, the module falls back to stdlib `json` transparently.

## Usage

```python
--8<-- "json/basic.py"
```

### Cache Integration

For caching, use the built-in serializer classes instead of the low-level functions. See the [cache serialization docs](cache.md#serialization) for `JsonSerializer`, `PydanticSerializer`, and `PickleSerializer`.

### Datetime Handling

`datetime` objects are automatically serialized to ISO 8601 strings. Note that deserialization returns a string, not a `datetime` object:

```python
--8<-- "json/datetime_handling.py"
```

## Performance

`orjson` is about 7 times faster than stdlib `json` for serialization. The module chooses the implementation at import time, so there is no per-call branching.

| Method | Speed |
|--------|-------|
| `orjson` (with `grelmicro[standard]`) | ~0.2 us/call |
| stdlib `json` (fallback) | ~1.5 us/call |

Use `has_orjson()` to check which backend is active at runtime:

```python
from grelmicro.json import has_orjson

if has_orjson():
    print("Using orjson")
else:
    print("Using stdlib json")
```

## API Reference

::: grelmicro.json
    options:
      show_root_heading: false
      members_order: source
