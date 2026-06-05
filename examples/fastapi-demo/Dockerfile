# Build context is the repository root so the demo runs against the
# grelmicro source in this checkout (not a published release).
FROM python:3.12-slim

WORKDIR /srv
RUN pip install --no-cache-dir uv

# Install grelmicro (current source) plus the demo's runtime deps.
COPY pyproject.toml README.md ./
COPY grelmicro ./grelmicro
RUN uv pip install --system --no-cache \
    ".[redis,postgres,standard]" \
    "fastapi>=0.115" "uvicorn[standard]>=0.30" "typer>=0.12"

COPY examples/fastapi-demo/app.py ./app.py

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
