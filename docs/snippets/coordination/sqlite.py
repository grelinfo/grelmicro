from grelmicro import Grelmicro
from grelmicro.coordination import Coordination
from grelmicro.providers.sqlite import SQLiteProvider

sqlite = SQLiteProvider("locks.db")
micro = Grelmicro(uses=[sqlite, Coordination(lock=sqlite)])
