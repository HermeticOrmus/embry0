"""The real app's route table for route-walking tests, with a floor (#502).

Since FastAPI 0.137, ``app.routes`` holds one opaque entry per included
router, so a test looping over it saw none of the ``/api/v1`` routes and
passed on nothing. Route-walking tests read the table here instead, and it
refuses to return a walk too short to be the whole app.
"""

from __future__ import annotations

from fastapi import FastAPI

from embry0.api.route_table import ServedRoute, served_routes

#: Far below what create_app() registers (157 routes, 152 of them API routes,
#: in October 2026) and far above what a walk missing the included routers
#: finds (6).
MIN_SERVED_ROUTES = 100


def app_routes(app: FastAPI) -> list[ServedRoute]:
    routes = served_routes(app.routes)
    assert len(routes) >= MIN_SERVED_ROUTES, (
        f"the route walk found {len(routes)} routes, fewer than {MIN_SERVED_ROUTES}: it is missing "
        "the included routers (#502), so a test looping over it would pass on nothing"
    )
    return routes
