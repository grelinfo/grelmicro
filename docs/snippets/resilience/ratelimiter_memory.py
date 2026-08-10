from grelmicro import Grelmicro
from grelmicro.providers.memory import MemoryProvider

memory = MemoryProvider()
micro = Grelmicro(uses=[memory])
