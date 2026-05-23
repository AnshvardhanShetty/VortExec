"""FastAPI app factory.

The maintainers (keyed by ``(venue, symbol)``) live entirely outside the
app's lifespan — they're owned by ``service.py``. The app just borrows a
reference via ``app.state.maintainers`` and routes pull it via DI. This
keeps the maintainer lifecycle (start/stop, signal handling) cleanly in
one place rather than split across orchestration and FastAPI startup.
"""

from __future__ import annotations

from fastapi import FastAPI

from vortexec.api.deps import MaintainersMap
from vortexec.api.routes import health, pretrade


def create_app(maintainers: MaintainersMap) -> FastAPI:
    app = FastAPI(
        title="VortExec",
        version="0.1.0",
        description="Live execution analytics for crypto algo traders.",
    )
    app.state.maintainers = maintainers
    app.include_router(health.router)
    app.include_router(pretrade.router)
    return app
