"""FastAPI dependency injection helpers.

The ``maintainers`` dict (keyed by ``(venue, symbol)``) is set on
``app.state`` by ``api.server.create_app`` and accessed by routes via
the ``MaintainersDep`` type alias.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from vortexec.maintainer.book_maintainer import BookMaintainer

MaintainerKey = tuple[str, str]
MaintainersMap = dict[MaintainerKey, BookMaintainer]


def get_maintainers(request: Request) -> MaintainersMap:
    maintainers: MaintainersMap = request.app.state.maintainers
    return maintainers


MaintainersDep = Annotated[MaintainersMap, Depends(get_maintainers)]
