# Kubernetes Backend

This page documents the internal design of the Kubernetes [Coordination backend](../coordination.md#backends).

## Lease Resources

The backend uses **Lease** resources from the `coordination.k8s.io/v1` API group. Leases are the idiomatic Kubernetes resource for coordination: they are used by kube-scheduler and client-go leader election. Unlike ConfigMaps, Leases have a dedicated schema for holder identity, duration, and timestamps.

### Field Mapping

| Lease Field | Lock Concept |
|---|---|
| `holderIdentity` | Lock token |
| `leaseDurationSeconds` | `ceil(duration)` |
| `acquireTime` / `renewTime` | Set on acquire/extend |

## Optimistic Concurrency

The backend uses Kubernetes **resourceVersion** for optimistic concurrency control:

- **Acquire**: GET the Lease. If 404 → CREATE. If expired or same token → REPLACE (with resourceVersion). If held by another → return `False`. On 409 Conflict → return `False`.
- **Release**: GET the Lease. If token matches and not expired → REPLACE clearing `holderIdentity`, `acquireTime`, and `renewTime` (vacate in place, never DELETE). Otherwise `False`.
- **Locked**: GET the Lease. Check `renewTime + leaseDurationSeconds >= now`.
- **Owned**: GET the Lease. Check `holderIdentity == token` and `renewTime + leaseDurationSeconds >= now`.

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

When multiple applications share the same Kubernetes namespace, use the `prefix` parameter to avoid lease name collisions (similar to Redis's `prefix` parameter). For example:

```python
backend = KubernetesLockAdapter(namespace="default", prefix="myapp-")
```

This prepends `myapp-` to every lease name before sanitization, ensuring different applications cannot interfere with each other's locks.

## Client Library

The backend uses [lightkube](https://lightkube.readthedocs.io/) as the Kubernetes async client. lightkube is lightweight, async-native (built on httpx), and provides strong typing for Kubernetes resources.

## Operational Assumptions

The Kubernetes backend depends on the cluster control plane for every primitive call. Plan deployments around these assumptions:

- **Single cluster**: Locks coordinate within one Kubernetes cluster. Multi-cluster fleets need a backend that fronts a shared store (Redis, Postgres).
- **API server availability**: Every `acquire`, `extend`, `release`, `locked`, and `owned` call hits the kube-apiserver. If the control plane is unreachable, the operation fails. Acquire-time errors surface as `LockAcquireError`. Pair the backend with retries or fall back to a Memory adapter for self-healing flows.
- **etcd latency**: Lease writes are replicated through etcd, so per-call latency is at least one Raft round-trip. Expect tens to low hundreds of milliseconds, not microseconds.
- **RBAC**: The Pod's ServiceAccount must hold a Role granting `get`, `create`, `update`, `delete`, and `list` on `leases.coordination.k8s.io` in the target namespace. A minimal Role is:

    ```yaml
    apiVersion: rbac.authorization.k8s.io/v1
    kind: Role
    metadata:
      name: grelmicro-leases
      namespace: default
    rules:
      - apiGroups: ["coordination.k8s.io"]
        resources: ["leases"]
        verbs: ["get", "list", "create", "update", "delete"]
    ```

    Bind it to the workload's ServiceAccount with a `RoleBinding` in the same namespace.

- **Clock skew**: Lease expiry uses the API server's clock via `renewTime + leaseDurationSeconds`. Large Node clock skew (>1 second) can produce surprising acquire/release decisions on Pods that compare against their local clock.
