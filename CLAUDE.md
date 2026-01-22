# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Automated algorithmic trading bot that trades S&P 500 ETFs comparing leveraged vs non-leveraged positions using the Trading 212 API, with data storage in Supabase.

**Key Business Logic:** The bot implements an "always invested" statistical arbitrage strategy that continuously holds either a non-leveraged S&P 500 ETF (1x) OR a leveraged version (3x). It monitors price divergence between the two assets and swaps positions based on mean reversion signals. The leveraged ETF is the default holding (for higher returns in bull markets), switching to non-leveraged when leveraged overperforms, and switching back when conditions normalize.

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

# Formatting
ruff format .

# Linting
ruff check .

# Type checking
pyright
```

## Architecture

### State Pattern (live_trading.py)
The bot uses a class-based State Pattern implementing an "always invested" strategy. Each state is a class inheriting from `TraderState` with a `process()` method that returns the next state:

```
TraderState (ABC)
├── Initializing        → HoldingLeveraged | HoldingNonLeveraged | self
├── HoldingLeveraged    → self | HoldingNonLeveraged
└── HoldingNonLeveraged → self | HoldingLeveraged
```

#### State Descriptions

**Initializing**
- Cancels open orders, determines current holdings
- If already holding non-leveraged → `HoldingNonLeveraged`
- If already holding leveraged → `HoldingLeveraged`
- If holding cash → buys leveraged (market order) → `HoldingLeveraged` (or stays in `Initializing` if order fails)

**HoldingLeveraged** (Default/Primary Holding)
- Holds the 3x leveraged ETF (higher returns in bull markets)
- Monitors for positive divergence (leveraged overperforming)
- If base price changes → update reference, stay
- If leveraged diverges UP > +0.4% for > 2 min → SWAP to `HoldingNonLeveraged`
  - Uses market orders; stays in current state if swap fails
  - Rationale: Leveraged overperforming, expect mean reversion

**HoldingNonLeveraged** (Temporary Hedge Position)
- Holds the 1x non-leveraged ETF as a hedge
- Monitors for multiple exit conditions:
  - If base price > entry price → SWAP to `HoldingLeveraged` (take profit)
  - If leveraged diverges DOWN < -0.4% for > 2 min → SWAP to `HoldingLeveraged` (capture recovery)
  - If stop-loss triggered (base < entry - 0.5%) → SWAP to `HoldingLeveraged`
  - If base price changed (not up) → update divergence reference, stay
- Uses market orders; stays in current state if swap fails

#### Divergence Calculation
```python
lev_diff_rel = (lev_current - lev_at_last_base_change) / lev_at_last_base_change
```
- Positive divergence: Leveraged went UP while base stayed flat → swap to non-leveraged
- Negative divergence: Leveraged went DOWN while base stayed flat → swap to leveraged

The main loop simply calls `trader_state.process(base_position, lev_position, curdatetime)` each iteration.

### Core Modules
- **sp500_bot.live_trading** - Main trading bot with State Pattern, market orders, Telegram notifications
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
LEV_DIFF_INVEST = 0.004        # 0.4% divergence threshold to trigger swap
TIME_DIFF_INVEST = timedelta(minutes=2)  # Min time before swapping
STOP_LOSS_THRESHOLD = 0.005   # 0.5% - swap if base drops this much below entry price
BASE_TICKER = Trading212Ticker.SP500_EUR    # 1x ETF (VUAAm_EQ)
LEV_TICKER = Trading212Ticker.SP500_EUR_L   # 3x ETF (US5Ld_EQ)
INTERVAL = 20  # seconds between trading loops
```

### Signal Data Structure
```python
class SignalData:
    time_last_base_change: datetime  # When base price last changed
    base_value_at_last_change: float  # Base price at that time (divergence reference)
    lev_value_at_last_change: float   # Lev price at that time (divergence reference)
    position_entry_price: float       # Entry price of current position (for P&L)
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
- State is not persisted; relies on position queries to recover after restart (Initializing state detects current holdings)
- Exchange schedules (TimeEvent objects) determine trading hours - trading blocked when markets closed
- Market orders used for all trades (immediate execution)
- Order verification via position value comparison after 5 second wait
- **Always Invested Strategy**: The bot never holds cash (except briefly during swaps). It always holds either leveraged (default) or non-leveraged ETF.
- **Bidirectional Swaps**: Unlike previous versions, the bot can swap in both directions based on divergence signals (positive divergence → non-leveraged, negative divergence → leveraged)
