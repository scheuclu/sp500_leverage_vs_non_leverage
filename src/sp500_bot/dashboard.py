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

from sp500_bot.models import Position
from sp500_bot.t212 import Trading212Ticker

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

    def __init__(
        self, code, num_shares_leverage=0, num_shares_non_leverage=0, cash=10000
    ):
        self.code = code
        self.num_shares_leverage = num_shares_leverage
        self.num_shares_non_leverage = num_shares_non_leverage
        self.cash = cash


@st.cache_data(ttl=300)
def load_data():
    """Load all position data from Supabase."""
    SUPABASE_KEY = os.environ["SUPABASE_KEY"]
    SUPABASE_URL = os.environ["SUPABASE_URL"]
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

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
            st.write(f"  {i+1}. {t.strftime('%H:%M:%S')} @ €{p:.2f}")

    if selected_data.sell_signal_times:
        st.write("**Sell Signals:**")
        for i, (t, p) in enumerate(zip(selected_data.sell_signal_times, sell_prices)):
            st.write(f"  {i+1}. {t.strftime('%H:%M:%S')} @ €{p:.2f}")

    if buy_prices and sell_prices:
        st.write("**Trade Results:**")
        for i, (b, s) in enumerate(zip(buy_prices, sell_prices)):
            pct = (s / b - 1) * 100
            st.write(f"  Trade {i+1}: Buy €{b:.2f} → Sell €{s:.2f} ({pct:+.2f}%)")
