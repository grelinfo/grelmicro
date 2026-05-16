from grelmicro import Grelmicro
from grelmicro.sync import Sync
from grelmicro.sync.kubernetes import KubernetesSyncAdapter

micro = Grelmicro(uses=[Sync(KubernetesSyncAdapter(namespace="default"))])
