"""GET /health — 200 if all maintainers report healthy, 503 otherwise."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from vortexec.api.deps import MaintainersDep
from vortexec.api.models import HealthResponse, MaintainerHealth

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health(maintainers: MaintainersDep, response: Response) -> HealthResponse:
    statuses = [
        MaintainerHealth(
            venue=venue,
            symbol=symbol,
            healthy=m.is_healthy(),
            resync_count=m.resync_count,
            drop_count=m.drop_count,
        )
        for (venue, symbol), m in maintainers.items()
    ]
    overall = bool(statuses) and all(s.healthy for s in statuses)
    if not overall:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="healthy" if overall else "unhealthy",
        maintainers=statuses,
    )
