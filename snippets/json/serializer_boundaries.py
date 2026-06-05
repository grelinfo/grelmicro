from grelmicro.json import json_dumps_bytes


# Sets, bytes, and custom objects are not JSON-encodable
class User:
    pass


try:
    json_dumps_bytes({"roles": {"admin", "user"}})
except TypeError as exc:
    print(exc)
    # Type is not JSON serializable: set

try:
    json_dumps_bytes({"user": User()})
except TypeError as exc:
    print(exc)
    # Type is not JSON serializable: User
