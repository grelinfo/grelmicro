# Build documentation
docs-build:
    uv run mkdocs build

# Start documentation dev server
docs-serve:
    uv run mkdocs serve

# Run the FastAPI demo (Redis + Postgres + app) via Docker Compose.
# Refuses to start when the port is taken, because the demo would come up
# healthy while another service answers on the URL this prints.
demo:
    #!/usr/bin/env bash
    set -euo pipefail
    port="${DEMO_PORT:-8000}"
    uv run --no-project -- python tools/demo_port.py --check "$port"
    DEMO_PORT="$port" docker compose -f examples/fastapi-demo/compose.yml \
        up --build --wait
    echo "Demo running: open http://127.0.0.1:${port}/docs"

# Stop and clean up the demo stack
demo-down:
    docker compose -f examples/fastapi-demo/compose.yml down -v

# Mutation-test the files in [tool.mutmut] only_mutate (set the target there).
# Uses tools/run_mutmut.py, which disables string-literal mutations (almost all
# log and exception message text here) and runs as `python -m mutmut` so the
# mutants/ copy shadows the editable install.
[doc("Mutation-test the files listed in [tool.mutmut] only_mutate")]
mutation:
    uv run python tools/run_mutmut.py run
    uv run python -m mutmut results

# List surviving mutants from the last mutation run.
mutation-results:
    uv run python -m mutmut results

# Run the full test tier the Release workflow runs, the way it runs it.
# The PR tier skips the slow marker, so this is the first place it runs
# locally. Coverage is appended across the three tiers because the 100%
# total is a claim about all of them together.
[doc("Run the full test tier the Release workflow runs")]
test-full:
    #!/usr/bin/env bash
    set -euo pipefail
    uv run pytest -x -m "not integration and not slow" --cov
    # Exit 5 is "no tests collected", which is fine while the tier is empty.
    uv run pytest -x -m "slow and not integration" --cov --cov-append || [ $? -eq 5 ]
    uv run pytest -x -m integration --cov --cov-append
    uv run coverage report --fail-under=100

# Bring the demo stack up, probe it the way CI does, and tear it down.
# Publishes a free port rather than 8000, so a port-forward or another
# local service never answers the probes in the demo's place.
[doc("Bring the demo stack up, probe it the way CI does, tear it down")]
demo-smoke:
    #!/usr/bin/env bash
    set -euo pipefail
    compose="examples/fastapi-demo/compose.yml"
    export DEMO_PORT="${DEMO_PORT:-$(uv run --no-project -- python tools/demo_port.py)}"
    trap 'docker compose -f "$compose" down -v >/dev/null 2>&1 || true' EXIT
    docker compose -f "$compose" up --build --wait --wait-timeout 240
    for path in livez readyz healthz; do
        curl -fsS "http://127.0.0.1:${DEMO_PORT}/${path}" > /dev/null
    done
    # The demo is the only service serving this path, so a probe that
    # reached something else fails here instead of reporting a pass.
    curl -fsS "http://127.0.0.1:${DEMO_PORT}/product/42" | grep -q '"Product 42"'
    echo "demo smoke passed on port ${DEMO_PORT}"

# Run the unit and integration tiers on every Python the release matrix uses.
# The version list comes from the workflow, so this cannot drift away from
# what CI runs. `test-full` already covers the primary Python with coverage,
# so this skips it and spends the time on the others.
[doc("Run the tests on every Python in the release matrix")]
test-matrix:
    #!/usr/bin/env bash
    set -euo pipefail
    primary="$(uv run python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    for version in $(uv run --no-project -- python tools/matrix_versions.py); do
        if [ "$version" = "$primary" ]; then
            echo "== Python $version covered by test-full, skipping"
            continue
        fi
        echo "== Python $version"
        uv run --isolated --python "$version" --all-extras --group dev \
            pytest -x -m "not integration and not slow"
        uv run --isolated --python "$version" --all-extras --group dev \
            pytest -x -m integration
    done

# Everything the Release workflow will run, before the tag exists.
# A release tag is immutable, so a failure found here costs nothing and the
# same failure found after tagging burns the version. The matrix is part of
# that: 0.34.0 passed every check this recipe ran and still failed the
# release, because the only Python it tested was the primary one.
[doc("Run everything the Release workflow will run, before the tag exists")]
release-check version: (release-check-fast version) test-matrix demo-smoke
    @echo "release-check passed for {{version}}"

# `release-check` without the demo tier, for iterating.
[doc("release-check without the demo tier, for iterating")]
release-check-fast version:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -n "$(git status --porcelain)" ]; then
        echo "working tree is not clean" >&2
        exit 1
    fi
    git fetch --quiet origin main
    if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
        echo "HEAD is not origin/main, so this is not what would be tagged" >&2
        exit 1
    fi
    uv run python tools/changelog.py check "{{version}}"
    just test-full
    uv run mkdocs build --strict
    echo "release-check-fast passed for {{version}}"

# Print a version's changelog section, which is the GitHub Release body.
release-notes version:
    @uv run python tools/changelog.py notes "{{version}}"

# Check a published version against the supply-chain claims in the README.
# The SLSA Build L2 badge is only true while provenance keeps being produced
# and keeps verifying, so this turns the badge into something anyone can
# check rather than something the project asserts.
[doc("Check a published version against the supply-chain claims")]
verify-release version:
    #!/usr/bin/env bash
    set -euo pipefail
    dir="$(mktemp -d)"
    trap 'rm -rf "$dir"' EXIT
    echo "== downloading grelmicro=={{version}} from PyPI"
    uv run --no-project -- python tools/download_release.py "{{version}}" "$dir"
    echo "== verifying build provenance"
    for artifact in "$dir"/*; do
        # `gh` is silent on success, so say what passed. Verification is
        # bound to the file digest: a tampered artifact finds no attestation.
        gh attestation verify "$artifact" --repo grelinfo/grelmicro
        echo "  verified $(basename "$artifact")"
    done
    echo "== verifying the version resolves, imports, and points at its docs"
    uv run --refresh --no-project --with "grelmicro=={{version}}" --with httpx \
        -- python tools/verify_release.py "{{version}}"
    echo "verify-release passed for {{version}}"
