# Reconfigure from a ConfigMap

[`reconfigure(new_config)`](../architecture/reconfigure.md) swaps a live component's configuration without rebuilding it. This page is the worked example: a Kubernetes `ConfigMap` watcher that reloads a `Lock`'s lease settings at runtime, plus a `SIGHUP` variant for non-Kubernetes hosts.

The example uses the official `kubernetes` client. grelmicro does not depend on it, so install it in your app: `pip install kubernetes`.

## The ConfigMap

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: ledger-lock
data:
  lease_duration: "30"
  retry_interval: "0.5"
```

## Watch and reconfigure

A background task watches the `ConfigMap` and calls `reconfigure` only when the parsed config actually changes. A malformed update logs a warning and keeps the previous config: `reconfigure` validates the new config and leaves the live one untouched if validation fails.

```python
import logging

from kubernetes import client, config, watch

from grelmicro.sync import Lock
from grelmicro.sync.lock import LockConfig

logger = logging.getLogger("reconfigure")

ledger_lock = Lock("ledger")


def parse(data: dict[str, str]) -> LockConfig:
    """Build a validated LockConfig from the ConfigMap data."""
    return LockConfig(
        lease_duration=float(data["lease_duration"]),
        retry_interval=float(data["retry_interval"]),
    )


async def watch_configmap(namespace: str = "default") -> None:
    config.load_incluster_config()
    api = client.CoreV1Api()
    applied: LockConfig | None = None

    for event in watch.Watch().stream(
        api.list_namespaced_config_map,
        namespace=namespace,
        field_selector="metadata.name=ledger-lock",
    ):
        data = event["object"].data or {}
        try:
            new_config = parse(data)
        except (KeyError, ValueError):
            logger.warning("Ignoring malformed ledger-lock ConfigMap")
            continue
        # Debounce: skip no-op updates so we only reconfigure on change.
        if new_config == applied:
            continue
        await ledger_lock.reconfigure(new_config)
        applied = new_config
        logger.info("Reapplied ledger-lock config: %s", new_config)
```

Run `watch_configmap` as a grelmicro background task:

```python
from grelmicro.task import Tasks

tasks = Tasks()
tasks.add_task(watch_configmap)
```

`reconfigure` keeps runtime state (held leases, in-flight acquires) across the swap, so a config reload never drops a lock. See [Live reconfiguration](../architecture/reconfigure.md) for the full contract.

## SIGHUP variant (no Kubernetes)

Outside Kubernetes, reload from a file on `SIGHUP`. The orchestrator (or an operator running `kill -HUP`) triggers the reload, and the same `reconfigure` contract applies.

```python
import asyncio
import json
import signal
from pathlib import Path

CONFIG_PATH = Path("/etc/ledger/lock.json")


def _request_reload(reload: asyncio.Event) -> None:
    reload.set()


async def watch_sighup() -> None:
    loop = asyncio.get_running_loop()
    reload = asyncio.Event()
    loop.add_signal_handler(signal.SIGHUP, _request_reload, reload)
    applied: LockConfig | None = None

    while True:
        await reload.wait()
        reload.clear()
        try:
            new_config = parse(json.loads(CONFIG_PATH.read_text()))
        except (OSError, KeyError, ValueError):
            logger.warning("Ignoring malformed lock config file")
            continue
        if new_config == applied:
            continue
        await ledger_lock.reconfigure(new_config)
        applied = new_config
```

The [FastAPI demo](https://github.com/grelinfo/grelmicro/tree/main/examples/fastapi-demo) is a good place to try the `SIGHUP` variant: it already runs background tasks, so adding `tasks.add_task(watch_sighup)` and sending `docker compose kill -s SIGHUP app` reloads the lock without a restart.
