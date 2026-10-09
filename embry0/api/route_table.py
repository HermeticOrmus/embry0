"""Every route as it is served: its full path template and the route object behind it.

FastAPI 0.137 stopped copying an included router's routes into the app (#502).
``app.routes`` now holds one opaque entry per ``include_router`` call, an
included ``APIRoute`` keeps its own unprefixed ``path``, and that unprefixed
route is what ``request.scope["route"]`` holds while it serves a request. Code
that read a template from either place lost the ``/api/v1`` prefix with no
error: the merge-scope allowlist refused every mutation, and walks over
``app.routes`` saw no included route at all.

``fastapi.routing.iter_route_contexts`` (0.137.2) yields every route with its
include prefixes applied. Before 0.137, ``app.routes`` already was that flat,
prefixed list. Read route templates through this module, so that difference
lives in one place.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import fastapi.routing
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from starlette.requests import HTTPConnection
from starlette.routing import BaseRoute

# None before fastapi 0.137, whose app.routes is already flat and prefixed.
# pyproject excludes 0.137.0 and 0.137.1: they nest routers but lack this.
_iter_route_contexts: Callable[[Sequence[BaseRoute]], Iterable[Any]] | None = getattr(
    fastapi.routing, "iter_route_contexts", None
)


@dataclass(frozen=True, slots=True)
class ServedRoute:
    """One route of the app, with every include prefix applied."""

    path: str
    path_regex: re.Pattern[str]
    #: None for a route that is not bound to methods: a WebSocket route or a mount.
    methods: frozenset[str] | None
    #: What ``request.scope["route"]`` holds while this route serves a request.
    route: BaseRoute
    #: The dependencies as served, the include-level ones among them.
    dependant: Dependant | None

    @property
    def is_api_route(self) -> bool:
        return isinstance(self.route, APIRoute)


def served_routes(routes: Sequence[BaseRoute]) -> list[ServedRoute]:
    """Flatten ``app.routes`` into the routes it serves, in matching order.

    A route without a path (a ``Host`` route) is left out: it has no template.
    """
    if _iter_route_contexts is None:
        return [entry for route in routes if (entry := _entry(route, served_by=route)) is not None]
    entries = []
    for context in _iter_route_contexts(routes):
        # For an API route the context itself answers with the prefixed path.
        # Any other route in an included router (a WebSocket route, a plain
        # Starlette route, a mount) is rebuilt with the prefix applied and kept
        # on ``starlette_route``, and the context's own ``path`` is empty
        # (fastapi 0.137.2 to 0.143 at least). The route table test pins it.
        rebuilt = getattr(context, "starlette_route", None)
        entry = _entry(context if rebuilt is None else rebuilt, served_by=context.original_route)
        if entry is not None:
            entries.append(entry)
    return entries


def _entry(view: Any, *, served_by: BaseRoute) -> ServedRoute | None:
    path = getattr(view, "path", None)
    path_regex = getattr(view, "path_regex", None)
    if not path or path_regex is None:
        return None
    methods = getattr(view, "methods", None)
    return ServedRoute(
        path=path,
        path_regex=path_regex,
        methods=frozenset(methods) if methods is not None else None,
        route=served_by,
        dependant=getattr(view, "dependant", None),
    )


def route_template(conn: HTTPConnection) -> str:
    """The full path template of the route serving ``conn``; ``""`` before routing.

    Never ``conn.scope["route"].path``: since FastAPI 0.137 that lacks the
    include prefix.
    """
    serving = conn.scope.get("route")
    if serving is None:
        return ""
    candidates = [entry for entry in served_routes(conn.app.routes) if entry.route is serving]
    if len(candidates) > 1:
        # One router included under several prefixes: the same route object
        # serves all of them, so the path decides which one this is.
        path = conn.scope.get("path", "")
        candidates = [entry for entry in candidates if entry.path_regex.match(path)]
    return candidates[0].path if candidates else ""
