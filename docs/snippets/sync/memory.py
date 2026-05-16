from grelmicro import Grelmicro
from grelmicro.sync import Sync
from grelmicro.sync.memory import MemorySyncAdapter

micro = Grelmicro(uses=[Sync(MemorySyncAdapter())])
