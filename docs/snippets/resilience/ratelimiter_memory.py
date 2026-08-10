from grelmicro import Grelmicro
from grelmicro.providers.memory import MemoryProvider

# Memory keeps state in the process: tests and single-process apps.
memory = MemoryProvider()
micro = Grelmicro(uses=[memory])
