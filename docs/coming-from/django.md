# Coming from Django

grelmicro is not a web framework and has no ORM. It runs inside FastAPI, Starlette, Litestar or FastStream. What it gives you maps to Django's cache, `on_commit`, django-ratelimit and the authentication and permission classes of Django REST framework. This page maps each one, says where the behavior differs, and lists what to use for the rest.

## Quick reference

| Django | grelmicro | Page |
|---|---|---|
| `settings.py` | Keyword arguments, `GREL_*` variables and mounted files, for grelmicro components only | [Configuration](../config.md) |
| `LOGGING` | `grelmicro.log.configure()` and `GREL_LOG_*` | [Logging](../logging/index.md) |
| `transaction.on_commit()` | `Outbox.publish(...)` inside the transaction, `@outbox.handler` | [Outbox](../outbox/index.md) |
| `cache.get_or_set(...)` | `TTLCache` and `@cached` | [Cache](../cache/index.md) |
| `@cache_page(60)` | `CachedResponse(ttl=60)` on the route | [HTTP cache](../http/cache.md) |
| django-ratelimit `@ratelimit` | `RateLimitedRequests` for the app, `RateLimited` for one route | [Rate limit](../http/rate-limit.md) |
| DRF throttling | `RateLimitedRequests(..., trusted=TrustedProxies([...]))` | [Rate limit](../http/rate-limit.md) |
| DRF `authentication_classes` | `AuthenticatedRequests` with a `JWTVerifier` | [Authentication](../http/authentication.md) |
| DRF `IsAuthenticated` and permission classes | Every route is authenticated once `AuthenticatedRequests` is registered, `Authenticated(scopes=[...])` for one route | [Authentication](../http/authentication.md) |
| `request.user` | `CurrentPrincipal` | [Authentication](../http/authentication.md) |
| The test client | Your framework's `TestClient` with `micro.fake()` | [Testing](../testing.md) |

## Concept differences

### The outbox survives a crash

`on_commit` runs the callback in the same process after the commit. A crash in between loses it. `Outbox.publish(...)` writes the message in the same transaction as your rows, and a relay delivers it at least once, with retries and a dead-letter state. The outbox needs PostgreSQL, through asyncpg or a SQLAlchemy `AsyncSession`. It does not take a Django connection.

### Configuration covers grelmicro, not your app

grelmicro reads `GREL_*` variables only for its own components, and only when `GREL_ENV_LOAD` is set. Keep your own settings in `pydantic-settings`, as [Advanced configuration](../advanced/config.md) shows. Django forbids changing settings at runtime. `ExternalConfig` applies a changed mounted file to running components on purpose.

### The response caps the cache entry

With `@cache_page`, the decorator's timeout wins over the response's `max-age`. grelmicro does the opposite: `Cache-Control: max-age=30` caps the entry at 30 seconds whatever the `ttl`. A response marked `no-store`, `no-cache` or `private` is never stored.

### A rate limit answers 429

django-ratelimit with `block=True` raises `PermissionDenied`, so the caller gets `403`. grelmicro answers `429` with `RateLimit` and `Retry-After` headers. It also refuses to start without `TrustedProxies` or a `key`, so callers behind a proxy never share one bucket.

### Middleware is added for you

You order Django's `MIDDLEWARE` list yourself. In grelmicro, a component such as `AuthenticatedRequests` adds its own ASGI middleware when `micro.install(app)` runs, in a fixed order.

## Side by side

### A side effect after the commit

=== "Django"

    ```python
    from django.db import transaction


    def sign_up(email):
        with transaction.atomic():
            user = User.objects.create(email=email)
            transaction.on_commit(lambda: send_welcome_email(user.email))
    ```

=== "grelmicro"

    ```python
    --8<-- "outbox/quickstart.py"
    ```

### A cached page

=== "Django"

    ```python
    from django.views.decorators.cache import cache_page


    @cache_page(60)
    def list_products(request):
        return JsonResponse(load_products(), safe=False)
    ```

=== "grelmicro"

    ```python
    --8<-- "http/cache.py"
    ```

### A rate limit

=== "Django"

    ```python
    from django_ratelimit.decorators import ratelimit


    @ratelimit(key="ip", rate="100/m", block=True)
    def list_orders(request):
        ...
    ```

=== "grelmicro"

    ```python
    --8<-- "http/rate_limit.py"
    ```

### Authentication and permissions

=== "Django REST framework"

    ```python
    REST_FRAMEWORK = {
        "DEFAULT_AUTHENTICATION_CLASSES": [
            "rest_framework_simplejwt.authentication.JWTAuthentication",
        ],
        "DEFAULT_PERMISSION_CLASSES": [
            "rest_framework.permissions.IsAuthenticated",
        ],
    }
    ```

=== "grelmicro"

    ```python
    --8<-- "http/authentication.py"
    ```

## False friends

| Word | In Django | In grelmicro |
|---|---|---|
| signals | An in-process dispatcher, such as `post_save` | Operating system signals such as `SIGTERM`, which the server handles |
| middleware | The `MIDDLEWARE` list you order | ASGI middleware a component adds through `micro.install(app)` |
| fixtures | Database rows loaded by `TestCase.fixtures` | pytest fixtures |
| tags | Template tags | Cache tags for bulk invalidation |
| container | Usually a Docker container | The `Grelmicro` app that opens and closes your components, or a Docker container |
| channels | Django Channels, for WebSockets | The two places a configuration warning goes: a Python warning and a log record |

## No equivalent, do this instead

- **The ORM**: use SQLAlchemy or SQLModel. `Outbox.publish` takes their `AsyncSession`.
- **Signals**: call the function. Use the [Outbox](../outbox/index.md) for work after a commit.
- **Management commands**: use argparse, Typer or Click. `python -m grelmicro check` checks your wiring in CI.
- **Object-level permissions**: check ownership in the handler, with `CurrentPrincipal`.
- **Login, sessions and the admin**: grelmicro verifies tokens from your identity provider. It issues none.
- **A task queue**: use Celery, Dramatiq or taskiq. grelmicro runs scheduled tasks inside the service.
- **An outbox on MySQL or SQLite**: the outbox supports PostgreSQL only.
