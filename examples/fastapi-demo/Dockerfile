# Build context is the repository root so the demo runs against the
# grelmicro source in this checkout (not a published release).
# Pinned by digest for reproducible builds. Dependabot bumps the digest.
FROM python:3.14-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6

WORKDIR /srv

# uv comes from its own image rather than pip, so it is pinned by digest like
# the base image is. Dependabot bumps both. A version-pinned `pip install` still
# resolves whatever the index serves for that version.
COPY --from=ghcr.io/astral-sh/uv:0.11.29@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc /uv /usr/local/bin/uv

# Install grelmicro (current source) plus the demo's runtime deps.
COPY pyproject.toml README.md ./
COPY grelmicro ./grelmicro
RUN uv pip install --system --no-cache \
    ".[redis,postgres,standard]" \
    "fastapi==0.139.0" "uvicorn[standard]==0.51.0" "typer==0.24.1"

COPY examples/fastapi-demo/app.py ./app.py

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
