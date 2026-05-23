# VortExec

Live execution analytics for crypto algo traders. Pre-trade cost estimates, post-trade fill analysis, and conditional tail risk across multiple venues — built around a deterministic order book engine with an ML layer for calibrated tail estimation and anomaly detection.

## Problem

Crypto traders execute size without knowing what their fill will cost or whether the fill they got was good. The available tools are split: enterprise platforms (Talos, CoinRoutes) start at several thousand dollars a month and target funds with proper engineering teams; retail bot tools (3Commas, Shrimpy) measure nothing about execution quality. There's no affordable, serious option for the algo traders and small shops in between — people running real strategies with real size who care about basis points but can't justify enterprise pricing.

This is also becoming a compliance issue. MiCA in the EU requires firms to document best execution from 2025, and most have nothing to do it with.

## What it does

**Pre-trade.** Live API. Send a `(side, size, symbol)` and get back the deterministic walk-through cost on each venue, the optimal split across venues, and a calibrated range showing the realistic spread of outcomes given current book conditions. Sub-100ms response time. Targeted at algo traders executing over seconds to minutes, not HFT.

**Post-trade.** Submit a fill (or connect a read-only API key) and get back how that fill compared to the optimal at the moment of execution. The system flags fills that were unusually expensive *given the conditions* — separating "the market was hard" from "you got a bad fill." Weekly reports surface execution leakage by venue, asset, and time of day.

**Under the hood.** A live order book maintainer keeps in-sync books across multiple exchanges by consuming WebSocket diff streams and reconciling against periodic REST snapshots. The deterministic walk runs on the live books for exact slippage estimates. A quantile regression layer, trained on book features, provides calibrated tail estimates and conditional anomaly scoring on top of the walk. As live data accumulates, the model is retrained periodically on the proprietary multi-venue dataset.

## Customer

Solo algo traders and one-to-five-person crypto trading shops trading $1M–$500M monthly volume on CEXs. The buyer is the person actually running the trades — typically an ex-quant or technical founder who already understands microstructure and is bitten by execution costs but doesn't have a clean way to measure them. Fund tier targets the same profile at slightly larger size, with weekly reports and compliance exports added.

## Pricing

| Tier | Price | What's included |
|---|---|---|
| Free | $0 | API, 100 queries/day, single venue |
| Pro | $199/mo | Multi-venue API, post-trade analysis, weekly email report |
| Fund | $999–2,000/mo | Pro + compliance exports, priority support, custom report formats |

Pure subscription. No transaction fees, no custody, no touching customer funds.

## What's built and what's coming

**Building now (next 4–6 weeks).** A single Python service that maintains live order books from Binance, OKX, and Bybit, records all market data continuously to disk for replay and post-trade analysis, and exposes a pre-trade API for cost estimates and a post-trade API for fill analysis. The deterministic walk is the source of truth for point estimates; a quantile regression layer trained on book features provides calibrated tail estimates and anomaly scoring. Customer signup, billing, and a basic dashboard ship alongside the API.

**Next (months 2–6).** Forecasting model trained on accumulated live data — predicting realised slippage at short horizons rather than compressing the deterministic walk. Replaces the existing quantile model as the headline ML feature once it demonstrates calibrated outperformance on held-out periods. Optimal split logic refined with real customer fill data. DEX integration evaluated based on customer demand.

**Later (months 6+).** Scale. MiCA compliance reporting. Enterprise contracts and platform integrations as the dataset and model mature.

## Why this works

The mid-market is genuinely underserved — affordable, serious execution analytics for crypto doesn't exist between the $20/mo retail tools and the $5K+/mo enterprise platforms. The ICP is technical enough to evaluate the product on its merits and self-serve onboard, which makes the price point achievable without a sales motion.

The product compounds with usage. Every customer's fills (with permission) feed the dataset that improves the model, which improves the product. Maintaining real-time synchronised state across multiple exchanges, each with different APIs and failure modes, is operational complexity that compounds over time.

The forecasting work, once trained on months of live multi-venue data, is the part that's genuinely hard for a competitor to match — they'd need both the infrastructure and the data, accumulated in parallel.

## Honest risks

**Data is the constraint, not engineering.** The forecasting model needs months of live multi-venue data before it can be trained well. Until then, the ML layer is doing calibrated tail estimation and anomaly detection — useful, but not the differentiator the model becomes once forecasting works.

**The mid-market hypothesis is unverified.** $199/mo for execution analytics is a price point I believe matches what serious algo traders pay for adjacent tools, but I haven't yet validated this with real customers. Early conversations and a beta cohort will resolve this faster than further building.

**Solo capacity.** This is one person building, alongside other commitments. Realistic shipping pace is 4–6 weeks to multi-venue MVP, not 4–6 days. The honest constraint on growth is how much of my own time I can give the project, not infrastructure cost.

## Status

Building this solo. Not raising. Looking for 5–10 algo traders or small shops to beta the product in the next 6 weeks in exchange for free access and feedback. If you trade crypto programmatically and care about execution quality, that's the audience.
