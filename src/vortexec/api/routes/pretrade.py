"""POST /v1/estimate — pre-trade slippage estimate against the live book."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from vortexec.api.deps import MaintainersDep
from vortexec.api.models import (
    EstimateRequest,
    EstimateResponse,
    FeaturesModel,
    SimResultModel,
)
from vortexec.core.features import extract_features
from vortexec.core.simulator import simulate_market_order
from vortexec.core.types import Side

router = APIRouter()


@router.post("/v1/estimate", response_model=EstimateResponse)
async def estimate(
    req: EstimateRequest, maintainers: MaintainersDep
) -> EstimateResponse:
    key = (req.venue, req.symbol)
    maintainer = maintainers.get(key)
    if maintainer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no maintainer for {req.venue}/{req.symbol}",
        )
    if not maintainer.is_healthy():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"maintainer for {req.venue}/{req.symbol} is unhealthy",
        )

    book = maintainer.get_book()
    side = Side.BUY if req.side == "buy" else Side.SELL
    sim = simulate_market_order(book, side, req.size)
    feats = extract_features(book)

    return EstimateResponse(
        venue=req.venue,
        symbol=req.symbol,
        side=req.side,
        size=req.size,
        deterministic=SimResultModel(
            avg_price=sim.avg_price,
            slippage_bps=sim.slippage_bps,
            unfilled_qty=sim.unfilled_qty,
            levels_consumed=sim.levels_consumed,
        ),
        features=FeaturesModel(
            mid_price=feats.mid_price,
            spread_bps=feats.spread_bps,
            depth_top_5_bids=feats.depth_top_5_bids,
            depth_top_5_asks=feats.depth_top_5_asks,
            depth_top_10_bids=feats.depth_top_10_bids,
            depth_top_10_asks=feats.depth_top_10_asks,
            imbalance=feats.imbalance,
        ),
    )
