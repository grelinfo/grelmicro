# JSON

- **Start here**: [JSON guide](../json.md)
- **Common recipes**: `json_dumps_bytes`, `json_dumps_str`, `json_loads`. Uses `orjson` when installed, stdlib `json` otherwise.

::: grelmicro.json
    options:
      members:
        - JSONEncodable
        - JSONDecodable
        - json_dumps_bytes
        - json_dumps_str
        - json_loads
        - json_default
        - has_orjson
