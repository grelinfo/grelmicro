"""Shared k3s container helpers for the Kubernetes backend tests.

Both `test_kubernetes.py` and `test_lock_backends.py` need a k3s API server.
They share the one container built here, started once per session by the
`k3s_kubeconfig` fixture in `conftest.py`.
"""

import time as time_module

from docker.errors import APIError
from testcontainers.core.container import DockerContainer

K3S_IMAGE = "rancher/k3s:v1.31.4-k3s1"
"""Pinned k3s image. An unpinned tag would make the suite drift with upstream."""

READY_TIMEOUT = 120
"""Seconds to wait for k3s readiness.

A control plane boots in a few seconds when the machine is idle and takes far
longer when it is not, so this is generous on purpose. It is a once-per-session
cost, and the tests that use it exempt fixture setup from their own timeout.
"""


def create_k3s_container() -> DockerContainer:
    """Build the k3s container, unstarted.

    Single construction point for the whole suite. Traefik and the metrics
    server are disabled because no test uses them and both slow the boot.

    `--disable-agent` leaves out the kubelet. The backend stores every lock in
    a `coordination.k8s.io` Lease, so the tests need the API server and nothing
    that runs a workload. It also keeps the suite working on a rootless
    container runtime, where the kubelet exits on `/dev/kmsg` and takes the
    server down with it a moment after readiness.
    """
    return (
        DockerContainer(K3S_IMAGE)
        .with_command(
            "server --disable-agent --disable=traefik,metrics-server"
            " --tls-san=127.0.0.1"
        )
        .with_kwargs(privileged=True, tmpfs={"/run": "", "/var/run": ""})
        .with_exposed_ports(6443)
    )


def container_report(container: DockerContainer) -> str:
    """Return the container state and tail of its logs, for a failed wait."""
    try:
        wrapped = container.get_wrapped_container()
        wrapped.reload()
        logs = wrapped.logs(tail=20).decode("utf-8", errors="replace")
    except APIError as error:
        return f"container state unavailable ({error})"
    return f"status={wrapped.status}, last logs:\n{logs}"


def wait_for_k3s(
    container: DockerContainer,
    timeout: float = READY_TIMEOUT,
) -> None:
    """Wait for k3s to be ready.

    Docker reports a container as started before it necessarily accepts a
    command. In that window the probe raises `APIError` instead of returning
    a non-zero exit code, so both mean "not ready yet" and are polled again.
    On timeout the container state and logs are reported, which separates a
    genuine k3s failure from a slow start.
    """
    start = time_module.time()
    while time_module.time() - start < timeout:
        try:
            exit_code, _ = container.exec("kubectl get --raw /readyz")
        except APIError:
            exit_code = None
        if exit_code == 0:
            return
        time_module.sleep(1)
    msg = f"k3s did not become ready within {timeout}s. {container_report(container)}"
    raise TimeoutError(msg)


def extract_kubeconfig(container: DockerContainer) -> str:
    """Extract the kubeconfig from the k3s container."""
    exit_code, output = container.exec("cat /etc/rancher/k3s/k3s.yaml")
    if exit_code != 0:
        msg = f"Failed to extract kubeconfig. {container_report(container)}"
        raise RuntimeError(msg)
    return output.decode()
