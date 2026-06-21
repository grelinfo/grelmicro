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

# Mutation-test the files in [tool.mutmut] only_mutate (set the target there).
# Must run as `python -m mutmut` so the mutants/ copy shadows the editable install.
mutation:
    uv run python -m mutmut run
    uv run python -m mutmut results

# List surviving mutants from the last mutation run.
mutation-results:
    uv run python -m mutmut results
