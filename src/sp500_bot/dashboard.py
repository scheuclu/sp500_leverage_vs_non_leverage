import math
import os
from datetime import date, datetime
from enum import Enum

import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv
from plotly.subplots import make_subplots
from pydantic import BaseModel
from supabase import Client, create_client

from sp500_bot.models import HistoricalOrder, Position, Side
from sp500_bot.t212 import Trading212Ticker, fetch_historical_orders

load_dotenv()

# Page config
st.set_page_config(layout="wide", page_title="SP500 Strategy Dashboard")
st.title("SP500 Leverage vs Non-Leverage Strategy")

# Constants
BASE_TICKER = Trading212Ticker.SP500_EUR.value
LEV_TICKER = Trading212Ticker.SP500_EUR_L.value
LEV_DIFF_THRESHOLD = 0.004  # 0.4% divergence threshold
MIN_SECONDS = 100  # Minimum seconds before signal triggers


class DateData(BaseModel):
    date: date
    times: list[datetime] = []
    leveraged_prices: list[float] = []
    non_leveraged_prices: list[float] = []
    buy_signal_times: list[datetime] = []
    sell_signal_times: list[datetime] = []
    lev_moves: list[float] = []


class State(Enum):
    INVESTED_IN_LEVERAGE = 0
    INVESTED_IN_NON_LEVERAGE = 1
    READY_TO_INVEST = 2

    def __init__(self, code, num_shares_leverage=0, num_shares_non_leverage=0, cash=10000):
        self.code = code
        self.num_shares_leverage = num_shares_leverage
        self.num_shares_non_leverage = num_shares_non_leverage
        self.cash = cash


def get_supabase_client() -> Client:
    """Get Supabase client."""
    SUPABASE_KEY = os.environ["SUPABASE_KEY"]
    SUPABASE_URL = os.environ["SUPABASE_URL"]
    return create_client(SUPABASE_URL, SUPABASE_KEY)


@st.cache_data(ttl=300)
def load_data():
    """Load all position data from Supabase."""
    supabase = get_supabase_client()

    all_data = []
    batch_size = 1000
    offset = 0

    while True:
        response = (
            supabase.table("data")
            .select("*")
            .order("created_at")
            .range(offset, offset + batch_size - 1)
            .execute()
        )
        rows = response.data
        if not rows:
            break
        all_data.extend(rows)
        offset += batch_size

    return all_data


@st.cache_data(ttl=60)
def load_state_data():
    """Load state history from Supabase."""
    supabase = get_supabase_client()

    all_states = []
    batch_size = 1000
    offset = 0

    while True:
        response = (
            supabase.table("state")
            .select("*")
            .order("created_at")
            .range(offset, offset + batch_size - 1)
            .execute()
        )
        rows = response.data
        if not rows:
            break
        all_states.extend(rows)
        offset += batch_size

    return all_states


@st.cache_data(ttl=300)
def load_historical_orders(selected_date: date) -> list[HistoricalOrder]:
    """Load historical orders for both tickers on a specific date."""
    orders: list[HistoricalOrder] = []
    for ticker in [Trading212Ticker.SP500_EUR, Trading212Ticker.SP500_EUR_L]:
        try:
            ticker_orders = fetch_historical_orders(
                ticker=ticker,
                start_date=selected_date,
                end_date=selected_date,
            )
            orders.extend(ticker_orders)
        except Exception:
            pass  # Skip if API call fails
    return orders


def process_data(all_data):
    """Process raw data into DateData structures."""
    unique_dates = sorted(
        list(set([datetime.fromisoformat(d["created_at"]).date() for d in all_data]))
    )

    data = {d: DateData(date=d) for d in unique_dates}

    for row in all_data:
        d = datetime.fromisoformat(row["created_at"]).date()
        t = datetime.fromisoformat(row["created_at"])
        positions = {
            Position.model_validate_json(p).ticker: Position.model_validate_json(p)
            for p in row["positions"]
        }
        if BASE_TICKER in positions and LEV_TICKER in positions:
            data[d].times.append(t)
            data[d].leveraged_prices.append(positions[LEV_TICKER].currentPrice)
            data[d].non_leveraged_prices.append(positions[BASE_TICKER].currentPrice)

    return data


def compute_signals(data: dict[date, DateData]):
    """Compute buy and sell signals based on strategy logic."""
    trader_state = State.READY_TO_INVEST

    for d, datedata in data.items():
        if len(datedata.non_leveraged_prices) == 0:
            continue

        current_base_value = datedata.non_leveraged_prices[0]
        lev_at_current_base_value = datedata.leveraged_prices[0]
        current_base_value_since = datedata.times[0]

        for base, lev, dt in zip(
            datedata.non_leveraged_prices, datedata.leveraged_prices, datedata.times
        ):
            if base == current_base_value:
                lev_move_rel = (lev - lev_at_current_base_value) / lev_at_current_base_value
                if (
                    trader_state == State.READY_TO_INVEST
                    and lev_move_rel > LEV_DIFF_THRESHOLD
                    and (dt - current_base_value_since).seconds > MIN_SECONDS
                ):
                    datedata.buy_signal_times.append(dt)
                    trader_state = State.INVESTED_IN_NON_LEVERAGE
                    trader_state.num_shares_non_leverage = trader_state.cash / base
                    trader_state.cash = 0
            else:
                if trader_state == State.INVESTED_IN_NON_LEVERAGE:
                    trader_state = State.READY_TO_INVEST
                    trader_state.cash = base * trader_state.num_shares_non_leverage
                    trader_state.num_shares_non_leverage = 0
                    datedata.sell_signal_times.append(dt)
                current_base_value = base
                current_base_value_since = dt
                lev_at_current_base_value = lev
                lev_move_rel = 0
            datedata.lev_moves.append(lev_move_rel)

    return trader_state


# Load and process data
with st.spinner("Loading data from Supabase..."):
    all_data = load_data()
    state_data = load_state_data()
    data = process_data(all_data)
    trader_state = compute_signals(data)

# Sidebar controls
st.sidebar.header("Controls")
selected_date = st.sidebar.selectbox(
    "Select Date",
    options=sorted(data.keys(), reverse=True),
    format_func=lambda x: x.strftime("%Y-%m-%d"),
)

# Get data for selected date
selected_data = data[selected_date]

# Load historical orders for selected date
with st.spinner("Loading historical orders..."):
    historical_orders = load_historical_orders(selected_date)

# Display current trader state (from live trading bot)
if state_data:
    latest_state = state_data[-1]
    st.subheader("Live Trading Bot Status")
    state_col1, state_col2, state_col3, state_col4 = st.columns(4)

    with state_col1:
        state_name = latest_state.get("state_name", "Unknown")
        # Color-code the state
        if state_name == "ReadyToInvest":
            st.metric("Current State", "🟢 " + state_name)
        elif state_name == "InvestedInNonLeverage":
            st.metric("Current State", "🔵 " + state_name)
        elif state_name == "Initializing":
            st.metric("Current State", "🟡 " + state_name)
        elif state_name == "OrderFailed":
            st.metric("Current State", "🔴 " + state_name)
        else:
            st.metric("Current State", state_name)

    with state_col2:
        base_val = latest_state.get("base_value_at_last_change", 0)
        st.metric("Base Price at Last Change", f"€{base_val:.2f}" if base_val else "N/A")

    with state_col3:
        lev_val = latest_state.get("lev_value_at_last_change", 0)
        st.metric("Lev Price at Last Change", f"€{lev_val:.2f}" if lev_val else "N/A")

    with state_col4:
        time_str = latest_state.get("time_last_base_change", "")
        if time_str:
            last_change_dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
            st.metric("Last Base Change", last_change_dt.strftime("%H:%M:%S"))
        else:
            st.metric("Last Base Change", "N/A")

    # Show state update timestamp
    created_at = latest_state.get("created_at", "")
    if created_at:
        created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        st.caption(f"State last updated: {created_dt.strftime('%Y-%m-%d %H:%M:%S')}")

    st.divider()

# Calculate performance metrics
buy_prices = [
    p
    for t, p in zip(selected_data.times, selected_data.non_leveraged_prices)
    if t in selected_data.buy_signal_times
]
sell_prices = [
    p
    for t, p in zip(selected_data.times, selected_data.non_leveraged_prices)
    if t in selected_data.sell_signal_times
]

# Metrics row
st.subheader("Performance Summary")
col1, col2, col3, col4 = st.columns(4)

with col1:
    st.metric("Buy Signals", len(selected_data.buy_signal_times))
with col2:
    st.metric("Sell Signals", len(selected_data.sell_signal_times))
with col3:
    if buy_prices and sell_prices and len(buy_prices) == len(sell_prices):
        factors = [s / b for b, s in zip(buy_prices, sell_prices)]
        daily_return = (math.prod(factors) - 1) * 100
        st.metric("Daily Return", f"{daily_return:.2f}%")
    else:
        st.metric("Daily Return", "N/A")
with col4:
    st.metric("Data Points", len(selected_data.times))

# Price chart
st.subheader("Price Chart")

fig = make_subplots(
    specs=[
        [{"secondary_y": True}],
        [{"secondary_y": False}],
    ],
    shared_xaxes=True,
    rows=2,
    cols=1,
    row_heights=[0.7, 0.3],
    vertical_spacing=0.05,
    subplot_titles=("Prices", "Leverage Divergence"),
)

# Non-leveraged price trace
fig.add_trace(
    go.Scatter(
        x=selected_data.times,
        y=selected_data.non_leveraged_prices,
        name=f"Non-Leveraged ({BASE_TICKER})",
        line=dict(color="blue"),
    ),
    row=1,
    col=1,
)

# Leveraged price trace
fig.add_trace(
    go.Scatter(
        x=selected_data.times,
        y=selected_data.leveraged_prices,
        name=f"Leveraged ({LEV_TICKER})",
        line=dict(color="orange"),
    ),
    secondary_y=True,
    row=1,
    col=1,
)

# Buy signals
for t in selected_data.buy_signal_times:
    fig.add_vline(x=t, line=dict(color="green", width=2, dash="dash"), row=1, col=1)

# Sell signals
for t in selected_data.sell_signal_times:
    fig.add_vline(x=t, line=dict(color="red", width=2, dash="dash"), row=1, col=1)

# Historical orders - separate by side and ticker
if historical_orders:
    for ticker_value, is_secondary, ticker_name in [
        (BASE_TICKER, False, "Base"),
        (LEV_TICKER, True, "Lev"),
    ]:
        for side, color in [(Side.BUY, "green"), (Side.SELL, "red")]:
            # Filter orders for this ticker and side
            filtered_orders = [
                o
                for o in historical_orders
                if o.order and o.order.ticker == ticker_value and o.order.side == side
            ]
            if not filtered_orders:
                continue

            # Prepare data for scatter
            times = []
            prices = []
            symbols = []
            hover_texts = []

            for ho in filtered_orders:
                order = ho.order
                if not order or not order.createdAt:
                    continue

                times.append(order.createdAt)
                # Use limit price or fill price if available
                price = order.limitPrice or (ho.fill.price if ho.fill else None) or order.stopPrice
                prices.append(price)

                # Filled = solid circle, not filled = open circle
                is_filled = ho.fill is not None
                symbols.append("circle" if is_filled else "circle-open")

                # Build hover text
                status = order.status.value if order.status else "Unknown"
                order_type = order.type.value if order.type else "Unknown"
                qty = order.quantity or 0
                filled_qty = order.filledQuantity or 0
                fill_status = "FILLED" if is_filled else "NOT FILLED"
                hover_texts.append(
                    f"<b>{side.value} Order ({ticker_name})</b><br>"
                    f"Status: {status} ({fill_status})<br>"
                    f"Type: {order_type}<br>"
                    f"Price: €{price:.2f}<br>"
                    f"Qty: {filled_qty}/{qty}<br>"
                    f"Time: {order.createdAt.strftime('%H:%M:%S')}"
                )

            fig.add_trace(
                go.Scatter(
                    x=times,
                    y=prices,
                    mode="markers",
                    name=f"{side.value} Orders ({ticker_name})",
                    marker=dict(
                        color=color,
                        size=12,
                        symbol=symbols,
                        line=dict(width=2, color=color),
                    ),
                    hovertemplate="%{text}<extra></extra>",
                    text=hover_texts,
                ),
                secondary_y=is_secondary,
                row=1,
                col=1,
            )

# Leverage divergence trace
fig.add_trace(
    go.Scatter(
        x=selected_data.times,
        y=selected_data.lev_moves,
        name="Lev Divergence",
        fill="tozeroy",
        line=dict(color="purple"),
    ),
    row=2,
    col=1,
)

# Add threshold line
fig.add_hline(
    y=LEV_DIFF_THRESHOLD,
    line=dict(color="green", width=1, dash="dot"),
    row=2,
    col=1,
)

fig.update_layout(
    height=700,
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    hovermode="x unified",
)
fig.update_yaxes(title_text="Non-Leveraged Price", secondary_y=False, row=1, col=1)
fig.update_yaxes(title_text="Leveraged Price", secondary_y=True, row=1, col=1)
fig.update_yaxes(title_text="Divergence", row=2, col=1)

st.plotly_chart(fig, use_container_width=True)

# Trade details (collapsible)
with st.expander("Trade Details"):
    if selected_data.buy_signal_times:
        st.write("**Buy Signals:**")
        for i, (t, p) in enumerate(zip(selected_data.buy_signal_times, buy_prices)):
            st.write(f"  {i + 1}. {t.strftime('%H:%M:%S')} @ €{p:.2f}")

    if selected_data.sell_signal_times:
        st.write("**Sell Signals:**")
        for i, (t, p) in enumerate(zip(selected_data.sell_signal_times, sell_prices)):
            st.write(f"  {i + 1}. {t.strftime('%H:%M:%S')} @ €{p:.2f}")

    if buy_prices and sell_prices:
        st.write("**Trade Results:**")
        for i, (b, s) in enumerate(zip(buy_prices, sell_prices)):
            pct = (s / b - 1) * 100
            st.write(f"  Trade {i + 1}: Buy €{b:.2f} → Sell €{s:.2f} ({pct:+.2f}%)")

# State timeline chart
if state_data:
    st.subheader("Trader State Timeline")

    # Prepare data for the chart
    state_times = []
    state_values = []
    state_names = []
    state_colors = []

    # Map states to numeric values and colors
    state_map = {
        "Initializing": 1,
        "ReadyToInvest": 2,
        "InvestedInNonLeverage": 3,
        "OrderFailed": 0,
    }
    color_map = {
        "Initializing": "yellow",
        "ReadyToInvest": "green",
        "InvestedInNonLeverage": "blue",
        "OrderFailed": "red",
    }

    for state_entry in state_data:
        created_at = state_entry.get("created_at", "")
        state_name = state_entry.get("state_name", "Unknown")
        if created_at and state_name in state_map:
            state_times.append(datetime.fromisoformat(created_at.replace("Z", "+00:00")))
            state_values.append(state_map[state_name])
            state_names.append(state_name)
            state_colors.append(color_map[state_name])

    if state_times:
        # Filter to selected date if desired
        selected_state_indices = [i for i, t in enumerate(state_times) if t.date() == selected_date]

        if selected_state_indices:
            filtered_times = [state_times[i] for i in selected_state_indices]
            filtered_values = [state_values[i] for i in selected_state_indices]
            filtered_names = [state_names[i] for i in selected_state_indices]
            filtered_colors = [state_colors[i] for i in selected_state_indices]
        else:
            # Show all data if no data for selected date
            filtered_times = state_times
            filtered_values = state_values
            filtered_names = state_names
            filtered_colors = state_colors
            st.caption("No state data for selected date, showing all available data")

        state_fig = go.Figure()

        # Add step line for state transitions
        state_fig.add_trace(
            go.Scatter(
                x=filtered_times,
                y=filtered_values,
                mode="lines+markers",
                line=dict(shape="hv", color="gray", width=1),
                marker=dict(
                    size=10,
                    color=filtered_colors,
                    line=dict(width=1, color="black"),
                ),
                text=filtered_names,
                hovertemplate="<b>%{text}</b><br>Time: %{x}<extra></extra>",
                name="State",
            )
        )

        # Update layout
        state_fig.update_layout(
            height=250,
            yaxis=dict(
                tickmode="array",
                tickvals=[0, 1, 2, 3],
                ticktext=["OrderFailed", "Initializing", "ReadyToInvest", "InvestedInNonLev"],
                range=[-0.5, 3.5],
            ),
            xaxis_title="Time",
            yaxis_title="State",
            hovermode="x unified",
            margin=dict(l=20, r=20, t=20, b=20),
        )

        st.plotly_chart(state_fig, use_container_width=True)

# State history (collapsible)
if state_data:
    with st.expander("State History (Recent)"):
        # Show last 20 state changes
        recent_states = state_data[-20:][::-1]  # Most recent first
        for state_entry in recent_states:
            state_name = state_entry.get("state_name", "Unknown")
            created_at = state_entry.get("created_at", "")
            base_val = state_entry.get("base_value_at_last_change", 0)
            lev_val = state_entry.get("lev_value_at_last_change", 0)

            if created_at:
                created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                time_str = created_dt.strftime("%Y-%m-%d %H:%M:%S")
            else:
                time_str = "N/A"

            # State emoji
            emoji = {
                "ReadyToInvest": "🟢",
                "InvestedInNonLeverage": "🔵",
                "Initializing": "🟡",
                "OrderFailed": "🔴",
            }.get(state_name, "⚪")

            st.write(
                f"{emoji} **{state_name}** @ {time_str} | Base: €{base_val:.2f}, Lev: €{lev_val:.2f}"
            )
