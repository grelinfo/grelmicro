from grelmicro.json import json_dumps_bytes, json_loads

# Serialize Python objects to JSON bytes
data = json_dumps_bytes({"id": 1, "name": "Alice"})
print(data)
# b'{"id":1,"name":"Alice"}'

# Deserialize JSON bytes back to Python objects
obj = json_loads(data)
print(obj)
# {"id": 1, "name": "Alice"}
