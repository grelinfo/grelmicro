"""Shared k3s container helpers for the Kubernetes backend tests.

Both `test_kubernetes.py` and `test_lock_backends.py` start a k3s container.
They previously carried their own copies of these helpers, which drifted apart
and had to be fixed twice.
"""

import time as time_module

from docker.errors import APIError
from testcontainers.core.container import DockerContainer

READY_TIMEOUT = 45
"""Seconds to wait for k3s readiness.

Must stay below the `pytest.mark.timeout` of any test using a k3s fixture.
Fixture setup runs under the first test's timeout, so an equal budget means
pytest-timeout fires at the same moment and hides the report below.
"""


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
