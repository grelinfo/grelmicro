from datetime import UTC, datetime

from grelmicro.json import json_dumps_bytes

# datetime objects are automatically serialized to ISO 8601
event = {
    "type": "login",
    "timestamp": datetime(2025, 6, 15, 10, 30, tzinfo=UTC),
}
data = json_dumps_bytes(event)
print(data)
