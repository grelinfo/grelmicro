# Build context is the repository root so the demo runs against the
# grelmicro source in this checkout (not a published release).
# Pinned by digest for reproducible builds. Dependabot bumps the digest.
FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

WORKDIR /srv
RUN pip install --no-cache-dir uv==0.11.29

# Install grelmicro (current source) plus the demo's runtime deps.
COPY pyproject.toml README.md ./
COPY grelmicro ./grelmicro
RUN uv pip install --system --no-cache \
    ".[redis,postgres,standard]" \
    "fastapi==0.139.0" "uvicorn[standard]==0.51.0" "typer==0.24.1"

COPY examples/fastapi-demo/app.py ./app.py

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
