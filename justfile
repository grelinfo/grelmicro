# Build documentation
docs-build:
    uv run mkdocs build

# Start documentation dev server
docs-serve:
    uv run mkdocs serve

# Run the FastAPI demo (Redis + Postgres + app) via Docker Compose
demo:
    docker compose -f examples/fastapi-demo/compose.yml up --build --wait
    @echo "Demo running: open http://localhost:8000/docs"

# Stop and clean up the demo stack
demo-down:
    docker compose -f examples/fastapi-demo/compose.yml down -v
