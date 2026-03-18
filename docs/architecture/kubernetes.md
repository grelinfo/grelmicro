# Kubernetes Backend

This page documents the internal design of the Kubernetes [Synchronization Backend](../sync.md#backend).

## Lease Resources

The backend uses **Lease** resources from the `coordination.k8s.io/v1` API group. Leases are the idiomatic Kubernetes resource for coordination — they are used by kube-scheduler and client-go leader election. Unlike ConfigMaps, Leases have a dedicated schema for holder identity, duration, and timestamps.

### Field Mapping

| Lease Field | Lock Concept |
|---|---|
| `holderIdentity` | Lock token |
| `leaseDurationSeconds` | `ceil(duration)` (informational) |
| `acquireTime` / `renewTime` | Set on acquire/extend |
| Annotation `grelmicro/expire-at` | Precise Unix timestamp (source of truth) |

## Sub-second Duration Handling

Kubernetes `leaseDurationSeconds` is an integer field, but grelmicro primitives accept `float` durations. While real-world lock durations are typically seconds to minutes, sub-second durations (e.g., `duration=0.01`) are essential for fast test execution. To support this, the precise expiry is stored as a **Unix timestamp annotation** (`grelmicro/expire-at`). The `leaseDurationSeconds` field is set to `ceil(duration)` for informational purposes only.

## Timestamp Strategy

Like the [SQLite backend](sqlite.md), the Kubernetes backend uses **Python-side wall-clock timestamps** (`time.time()`). The annotation stores the precise expiry as a float string (e.g., `"1710000000.123456"`).

The same trade-offs apply: `time.time()` can drift with NTP corrections, but this is acceptable for lock durations typically measured in seconds to minutes.

## Optimistic Concurrency

The backend uses Kubernetes **resourceVersion** for optimistic concurrency control:

- **Acquire**: GET the Lease. If 404 → CREATE. If expired or same token → REPLACE (with resourceVersion). If held by another → return `False`. On 409 Conflict → return `False`.
- **Release**: GET the Lease. If token matches and not expired → DELETE. Otherwise `False`.
- **Locked**: GET the Lease. Check `expire_at >= now()`.
- **Owned**: GET the Lease. Check `holderIdentity == token` and `expire_at >= now()`.

The REPLACE operation includes the `resourceVersion` from the GET, so the API server rejects the update with a 409 Conflict if another client modified the Lease in between. This provides the same atomicity guarantees as database transactions.

## Lease Labeling

All Lease resources created by grelmicro are labeled with `app.kubernetes.io/managed-by: grelmicro`. This label is used during cleanup (`__aexit__`) to filter only grelmicro-managed leases, preventing accidental deletion of leases owned by kube-scheduler or other controllers.

## Name Sanitization

Lock names (e.g., `"lock:my-resource"`) must become valid Kubernetes resource names per RFC 1123:

- Lowercase alphanumeric characters and hyphens only
- Must start and end with an alphanumeric character
- Maximum 253 characters

The sanitization strategy replaces invalid characters with hyphens, collapses consecutive hyphens, truncates to 253 characters, and strips leading/trailing hyphens. The resulting lease names are human-readable and visible via `kubectl get lease`.

## Multi-App Isolation

When multiple applications share the same Kubernetes namespace, use the `prefix` parameter to avoid lease name collisions — similar to Redis's `prefix` parameter. For example:

```python
backend = KubernetesSyncBackend(namespace="default", prefix="myapp-")
```

This prepends `myapp-` to every lease name before sanitization, ensuring different applications cannot interfere with each other's locks.

## Client Library

The backend uses [lightkube](https://lightkube.readthedocs.io/) as the Kubernetes async client. lightkube is lightweight, async-native (built on httpx), and provides strong typing for Kubernetes resources.

## Lock Cleanup

On exit (`__aexit__`), the backend lists all Lease resources labeled `app.kubernetes.io/managed-by: grelmicro` in the configured namespace and deletes each expired lease individually. Unlike the SQL backends, the Kubernetes API does not support bulk conditional deletion, so leases are cleaned up one at a time. `NOT_FOUND` errors are silently ignored to handle concurrent deletions.
