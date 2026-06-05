# grelmicro FastAPI demo

A runnable FastAPI service that exercises every grelmicro Pattern against real Redis and Postgres, wired with Docker Compose. From a fresh clone it takes three commands.

## Run it

```bash
cd examples/fastapi-demo
docker compose up --wait
open http://localhost:8000/docs
```

`up --wait` blocks until Redis, Postgres, and the app are all healthy. From the repo root you can also run `just demo`.

## Hit the endpoints

```bash
# Cache: the second call within 30s is served from cache
curl localhost:8000/product/42

# Rate limiter: token bucket, 5 burst then 1/s per client
for i in $(seq 1 7); do curl -s "localhost:8000/quote?client=alice" -o /dev/null -w "%{http_code}\n"; done

# Circuit breaker: force failures, watch it open
curl "localhost:8000/flaky?fail=true"

# Distributed lock: serialize a ledger update across replicas
curl -X POST "localhost:8000/ledger?amount=100"

# Health probes
curl localhost:8000/livez   # process is up
curl localhost:8000/readyz  # Redis reachable
```

Watch the logs to see the local heartbeat task and the leader-only sweep:

```bash
docker compose logs -f app
```

## The Patterns

| Endpoint / task | Pattern |
|---|---|
| `GET /product/{id}` | **Cache** (`@cached` over the Redis `Cache` backend) |
| `GET /quote` | **Rate limiter** (Redis token bucket) |
| `GET /flaky` | **Circuit breaker** (Postgres, fleet-wide state) |
| `POST /ledger` | **Distributed lock** (Redis) |
| `nightly_sweep` task | **Leader election** (runs on one replica) |
| `heartbeat` task | **Interval task** (runs on every replica) |
| `GET /livez` `/readyz` `/healthz` | **Health checks** |

Read [`app.py`](app.py): every endpoint is a few lines with a comment naming its Pattern.

## Tear down

```bash
docker compose down -v
```
