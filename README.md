# VortExec

Live execution analytics for crypto algo traders. Pre-trade cost estimates, post-trade fill analysis, and conditional tail risk across multiple venues.

See [PRODUCT.md](PRODUCT.md) for what the product is and who it's for. See [ARCHITECTURE.md](ARCHITECTURE.md) for how the codebase is structured.

## Status

In active development. Not yet shipping. See [PRODUCT.md](PRODUCT.md) for the current build phase.

## Requirements

- Python 3.11+
- `uv` (recommended) or pip for dependency management

## Setup

```bash
# Clone and enter the repo
git clone <repo-url> vortexec
cd vortexec

# Install dependencies with uv
uv sync

# Or with pip
pip install -e ".[dev]"

# Copy the env template and fill in values
cp .env.example .env
```

## Configuration

Configuration is loaded from YAML files in `config/` with overrides from environment variables. The default config is `config/default.yaml`; use `VORTEXEC_ENV=dev` or `VORTEXEC_ENV=prod` to load environment-specific overrides.

Secrets (Stripe keys, exchange credentials for testnet) come from environment variables, never YAML.

## Running

```bash
# Run the service locally
python -m vortexec

# With a specific config environment
VORTEXEC_ENV=dev python -m vortexec
```

The service starts the live book maintainers, the recorder, and the HTTP API together. Logs go to stdout in structured JSON format.

## Development

```bash
# Run tests
pytest

# Run unit tests only (fast)
pytest tests/unit

# Type checking
mypy --strict src/

# Linting and formatting
ruff check src/ tests/
ruff format src/ tests/
```

Integration tests in `tests/integration/` connect to live exchange APIs. They're slow and rate-limited, so they're skipped by default. Run them explicitly with:

```bash
pytest tests/integration --run-live
```

## Project layout

```
src/vortexec/        # production code
tests/               # unit and integration tests
research/            # ML research (separate dependencies)
scripts/             # one-off utilities
config/              # YAML config files
data/                # gitignored: recorded market data and model files
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for detail on each module.

## Documents

- [PRODUCT.md](PRODUCT.md) — what the product is, who it's for, pricing, roadmap
- [ARCHITECTURE.md](ARCHITECTURE.md) — codebase structure and conventions
- [DECISIONS.md](DECISIONS.md) — running log of design decisions and rationale

## License

Proprietary. All rights reserved.
