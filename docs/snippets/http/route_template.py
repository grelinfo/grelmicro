from fastapi import APIRouter, FastAPI
from starlette.types import ASGIApp, Receive, Scope, Send

from grelmicro import Grelmicro
from grelmicro.http import route_template

seen: set[str] = set()


class Coverage:
    """Record each route template a request reached."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        await self.app(scope, receive, send)
        if scope["type"] == "http":
            route = route_template(scope)
            if route is not None:
                seen.add(route)


recipes = APIRouter(prefix="/recipes")


@recipes.get("/{recipe_id}")
async def recipe(recipe_id: int) -> dict[str, int]:
    return {"id": recipe_id}


app = FastAPI()
app.include_router(recipes, prefix="/api/meals/v1")
app.add_middleware(Coverage)

micro = Grelmicro()
micro.install(app)
