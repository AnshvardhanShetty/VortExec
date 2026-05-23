"""Pydantic request/response models for the HTTP API. The API contract."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ─── /health ──────────────────────────────────────────────────────────────────


class MaintainerHealth(BaseModel):
    venue: str
    symbol: str
    healthy: bool
    resync_count: int
    drop_count: int


class HealthResponse(BaseModel):
    status: Literal["healthy", "unhealthy"]
    maintainers: list[MaintainerHealth]


# ─── /v1/estimate ─────────────────────────────────────────────────────────────


class EstimateRequest(BaseModel):
    venue: Literal["binance"] = "binance"
    symbol: str = Field(min_length=1)
    side: Literal["buy", "sell"]
    size: float = Field(gt=0)


class SimResultModel(BaseModel):
    avg_price: float | None
    slippage_bps: float | None
    unfilled_qty: float
    levels_consumed: int


class FeaturesModel(BaseModel):
    mid_price: float | None
    spread_bps: float | None
    depth_top_5_bids: float
    depth_top_5_asks: float
    depth_top_10_bids: float
    depth_top_10_asks: float
    imbalance: float | None


class EstimateResponse(BaseModel):
    venue: str
    symbol: str
    side: Literal["buy", "sell"]
    size: float
    deterministic: SimResultModel
    features: FeaturesModel
