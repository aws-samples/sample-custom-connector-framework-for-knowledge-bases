"""Registers one Powertools route per operation in the catalog, each running its activity through the router."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping

from aws_lambda_powertools.event_handler import APIGatewayRestResolver, Response
from ccf_api_models.catalog import OperationCatalog
from ccf_api_models.operations import Operation

from common.operation import Activity
from common.router import Route, RouterAdapter

type RouteHandler = Callable[..., Response[str]]
"""A Powertools route function; it receives the path members as keyword arguments, which the binder reads instead."""

_PATH_MEMBER = re.compile(r"\{([A-Za-z0-9_]+)\}")


class RouteTable:
    def __init__(self, app: APIGatewayRestResolver, catalog: OperationCatalog, router: RouterAdapter) -> None:
        self._app = app
        self._catalog = catalog
        self._router = router

    def register(self, activities: Mapping[Operation, Activity]) -> None:
        """Add one route per catalog spec; raise ValueError naming each operation without an activity and each
        activity without an operation."""
        specs = {spec.operation: spec for spec in self._catalog.specs()}
        missing = sorted(specs.keys() - activities.keys())
        unknown = sorted(activities.keys() - specs.keys())
        if missing or unknown:
            raise ValueError(f"Operations without an activity: {missing}; activities without an operation: {unknown}")
        for operation, spec in specs.items():
            route = Route(spec=spec, activity=activities[operation])
            self._app.route(_PATH_MEMBER.sub(r"<\1>", spec.path), spec.method.value)(self._handler(route))

    def _handler(self, route: Route) -> RouteHandler:
        router = self._router

        def handle(**_path_members: str) -> Response[str]:
            return router.execute(route)

        return handle
