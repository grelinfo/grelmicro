"""Shared fixtures for the coordination tests.

The k3s container lives here because two modules need the same API server.
Every test that touches it carries `pytest.mark.xdist_group("k3s")`, so
`--dist loadgroup` sends them all to one worker and this fixture builds one
container per session. Without the group, each worker that happened to receive
a Kubernetes test would boot a control plane of its own.
"""

from collections.abc import Generator

import pytest

from tests.coordination._k3s import (
    create_k3s_container,
    extract_kubeconfig,
    wait_for_k3s,
)


@pytest.fixture(scope="session")
def k3s_kubeconfig(tmp_path_factory: pytest.TempPathFactory) -> Generator[str]:
    """Start k3s once and yield the path to a kubeconfig pointing at it."""
    with create_k3s_container() as container:
        wait_for_k3s(container)
        kubeconfig = extract_kubeconfig(container).replace(
            "https://127.0.0.1:6443",
            f"https://127.0.0.1:{container.get_exposed_port(6443)}",
        )
        path = tmp_path_factory.mktemp("k3s") / "kubeconfig.yaml"
        path.write_text(kubeconfig)
        yield str(path)
