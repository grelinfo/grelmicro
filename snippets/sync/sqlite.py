from grelmicro import Grelmicro
from grelmicro.sync import Sync
from grelmicro.sync.sqlite import SQLiteSyncAdapter

micro = Grelmicro(uses=[Sync(SQLiteSyncAdapter("locks.db"))])
