"""Pick the host port the demo stack publishes.

`just demo-smoke` probes the demo over a published port. Anything else
already listening on that port answers the probes instead, and the smoke
check then passes or fails against the wrong service. A local
`kubectl port-forward` or `oc port-forward` on 8000 is enough to do it,
and it does not even collide visibly: it binds `127.0.0.1` while the
container runtime binds the wildcard address, so both start and the
probes reach the port-forward.

With no arguments, print a free ephemeral port. With `--check PORT`,
exit non-zero when that port is already taken.

Run via `just demo` and `just demo-smoke`.
"""

from __future__ import annotations

import argparse
import socket
import sys


def free_port(attempts: int = 20) -> int:
    """Return a port free on both loopback families right now.

    The kernel hands out an ephemeral port for one family at a time, and a
    port free on `127.0.0.1` can be taken on `::1`. Each candidate is
    checked against both before it is returned.

    Raises:
        SystemExit: If no candidate came back free within `attempts`.
    """
    for _ in range(attempts):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if is_free(port):
            return port
    msg = f"found no port free on both loopback families in {attempts} tries"
    raise SystemExit(msg)


def is_free(port: int) -> bool:
    """Return whether a port can be bound on both loopback families."""
    for family, address in (
        (socket.AF_INET, "127.0.0.1"),
        (socket.AF_INET6, "::1"),
    ):
        try:
            with socket.socket(family) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((address, port))
        except OSError:
            return False
    return True


def main() -> int:
    """Print a free port, or check the one given."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", type=int, default=None)
    args = parser.parse_args()

    if args.check is None:
        print(free_port())
        return 0

    if is_free(args.check):
        return 0
    print(
        f"port {args.check} is already in use, "
        f"so the demo would not be the service answering there. "
        f"Free it, or pick another: DEMO_PORT=8001 just demo",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
