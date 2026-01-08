# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Automated algorithmic trading bot that trades S&P 500 ETFs comparing leveraged vs non-leveraged positions using the Trading 212 API, with data storage in Supabase.

**Key Business Logic:** The bot implements a statistical arbitrage strategy that monitors price differences between a non-leveraged S&P 500 ETF (1x) and a leveraged version (3x). When the leveraged asset diverges >0.4% from expected behavior over 2+ minutes, the bot places buy/sell orders to capitalize on mean reversion patterns.

## Project Structure

```
src/sp500_bot/           # Main package
├── __init__.py
├── live_trading.py      # Main trading bot with state machine
├── t212.py              # Trading 212 API wrapper
├── models.py            # Pydantic models (auto-generated)
├── utils.py             # Exchange schedule utilities
├── sb.py                # Supabase database helper
├── tgbot.py             # Telegram notifications
├── ingestion.py         # Data collection to Supabase
├── dashboard.py         # Streamlit visualization
└── read.py              # Historical analysis
```

## Commands

```bash
# Install dependencies
uv sync

# Run the live trading bot
python -m sp500_bot.live_trading

# Run data ingestion
python -m sp500_bot.ingestion

# Launch dashboard visualization
streamlit run src/sp500_bot/dashboard.py

# Linting
ruff check .

# Type checking
pyright
```

## Architecture

### State Pattern (live_trading.py)
The bot uses a class-based State Pattern. Each state is a class inheriting from `TraderState` with a `process()` method that returns the next state:

```
TraderState (ABC)
├── Initializing           → ReadyToInvest
├── ReadyToInvest          → self | OrderFailed | InvestedInNonLeverage
├── InvestedInNonLeverage  → self | Initializing | OrderFailed
└── OrderFailed            → Initializing
```

State transitions:
- `Initializing`: Cancels open orders, sells excess holdings → `ReadyToInvest`
- `ReadyToInvest`: Monitors leveraged asset divergence, places buy order when threshold met → `InvestedInNonLeverage`
- `InvestedInNonLeverage`: Waits for base price change, places sell order → `Initializing`
- `OrderFailed`: Recovery state → `Initializing`

The main loop simply calls `trader_state.process(base_position, lev_position, curdatetime)` each iteration.

### Core Modules
- **sp500_bot.live_trading** - Main trading bot with State Pattern, limit orders, Telegram notifications
- **sp500_bot.t212** - Trading 212 API wrapper with built-in rate limiting
- **sp500_bot.models** - Auto-generated Pydantic models from api.json using datamodel-codegen
- **sp500_bot.utils** - Exchange schedule utilities (market open checks)
- **sp500_bot.sb** - Supabase database helper for writing position snapshots
- **sp500_bot.tgbot** - Telegram notification sender

### Rate Limiting (t212.py)
The `RateLimiter` class automatically enforces Trading 212 API rate limits. Each function calls `_rate_limiter.wait(endpoint)` before making a request, which sleeps if insufficient time has passed since the last call.

| Endpoint | Limit | Functions |
|----------|-------|-----------|
| `portfolio` | 5s | `fetch_positions()` |
| `portfolio_ticker` | 1s | `fetch_single_holding()` |
| `account_cash` | 2s | `fetch_account_cash()` |
| `orders_get` | 5s | `fetch_open_orders()` |
| `order_by_id` | 1s | `fetch_open_order()`, `has_order_been_filled()` |
| `orders_limit` | 2s | `place_limit_order()` |
| `orders_market` | 1.2s | `place_buy_order()`, `place_market_order()` |
| `orders_stop` | 2s | `place_sell_order()` |
| `orders_cancel` | 1.2s | `cancel_order_by_id()` |
| `instruments` | 50s | `fetch_instruments()` |
| `exchanges` | 30s | `fetch_exchanges()` |

Rate limits are defined in `RateLimiter.LIMITS` and derived from `api.json`.

### Data Flow
1. Real-time polling via Trading 212 API (20-second intervals)
2. Position snapshots stored in Supabase "data" table
3. Historical analysis via read.py, visualization via dashboard.py (Streamlit + Plotly)

### Key Parameters (live_trading.py)
```python
LEV_DIFF_INVEST = 0.004        # 0.4% divergence threshold to trigger buy
TIME_DIFF_INVEST = timedelta(minutes=2)  # Min time before buying
STOP_LOSS_THRESHOLD = 0.005   # 0.5% - sell if base drops this much below buy price
BASE_TICKER = Trading212Ticker.SP500_EUR    # 1x ETF (VUAAm_EQ)
LEV_TICKER = Trading212Ticker.SP500_EUR_L   # 3x ETF (US5Ld_EQ)
INTERVAL = 20  # seconds between trading loops
```

## Technology Stack

- **Python 3.13+** with uv package manager
- **Trading API:** Trading 212 (demo.trading212.com/api/v0)
- **Database:** Supabase (PostgreSQL + PostgREST)
- **Visualization:** Streamlit + Plotly
- **Code Quality:** Ruff (linting), Pyright (type checking)
- **Notifications:** Telegram Bot API
- **Deployment:** Docker + Fly.io

## Environment Variables

Required in `.env`:
- `TRADING212_KEY` - Trading 212 API token
- `SUPABASE_KEY` - Database write access
- `SUPABASE_URL` - Database endpoint

## Important Notes

- Uses demo.trading212.com, not production API
- State is not persisted; relies on position queries to recover after restart
- Exchange schedules (TimeEvent objects) determine trading hours - trading blocked when markets closed
- Limit orders preferred over market orders (with 3-minute timeout before cancellation)
- Order fulfillment detection via 404 response (filled orders return 404)
