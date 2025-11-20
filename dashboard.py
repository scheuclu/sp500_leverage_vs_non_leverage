import os
import plotly.graph_objects as go
import requests
from models import TradableInstrument, Exchange, WorkingSchedule, Position
from dotenv import load_dotenv
from plotly.subplots import make_subplots
import plotly.io as pio
import math

pio.renderers.default = "browser"
import numpy as np
import pandas as pd
from supabase import Client, create_client
from datetime import datetime, timedelta, date
from pydantic import BaseModel
from plotly.subplots import make_subplots
from t212 import Trading212Ticker
load_dotenv()

import streamlit as st

st.set_page_config(layout="wide")
st.title("Strategy visualization")

TRADING212_KEY = os.environ["TRADING212_KEY"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
headers = {
    "Authorization": TRADING212_KEY,
    "Content-Type": "application/json",
}


BASE_TICKER=Trading212Ticker.SP500_EUR.value
LEV_TICKER=Trading212Ticker.SP500_EUR_L.value

# url = "https://demo.trading212.com/api/v0/equity/portfolio"
# response = requests.get(url, headers=headers)
# positions = [Position(**i) for i in response.json()]


date_str = "2025-10-28T08:47:00.425164+00:00"
dt = datetime.fromisoformat(date_str)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

FX = 0.9999
MIN_MOVE = 8

MIN_SECONDS = 100

###
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


unique_dates = sorted(
    list(set([datetime.fromisoformat(d["created_at"]).date() for d in all_data]))
)


class DateData(BaseModel):
    date: date
    times: list[datetime] = []
    leveraged_prices: list[float] = []
    non_leveraged_prices: list[float] = []
    buy_signal_times: list[datetime] = []
    sell_signal_times: list[datetime] = []
    lev_moves: list[float] = []


data = {d: DateData(date=d) for d in unique_dates}

for row in all_data:
    # positions= [Position.model_validate_json(p) for p in row['positions']]
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


# Compute buy and sell signals
from enum import Enum


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


trader_state = State.READY_TO_INVEST
for d, datedata in data.items():
    current_base_value = datedata.non_leveraged_prices[0] if len(datedata.non_leveraged_prices)>0 else []
    lev_at_current_base_value = datedata.leveraged_prices[0] if len(datedata.leveraged_prices)>0 else []
    current_base_value_since = datedata.times[0] if len(datedata.times)>0 else []
    for base, lev, dt in zip(
        datedata.non_leveraged_prices, datedata.leveraged_prices, datedata.times
    ):
        if base == current_base_value:
            lev_move_rel = (lev - lev_at_current_base_value)/lev_at_current_base_value
            if (
                trader_state == State.READY_TO_INVEST
                and lev_move_rel > 0.004
                and (dt - current_base_value_since).seconds > MIN_SECONDS
            ):
                datedata.buy_signal_times.append(dt)
                trader_state = State.INVESTED_IN_NON_LEVERAGE
                trader_state.num_shares_non_leverage = trader_state.cash / base
                trader_state.cash = 0
                # buys['x'].append(dt)
                # buys['y'].append(base)
            # if trader_state==State.READY_TO_INVEST and  lev_move < -10 and (dt - current_base_value_since).seconds > MIN_SECONDS:
            #     # sells['x'].append(dt)
            #     # sells['y'].append(base)
            #     datedata.sell_signal_times.append(dt)
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

st.text(
    str(
        [
            trader_state.cash,
            trader_state.num_shares_leverage,
            trader_state.num_shares_non_leverage,
        ]
    )
)

selected_date = st.selectbox(label="aaa", options=data.keys())


st.text(str(data[selected_date].buy_signal_times))
buy_prices = [
    p
    for t, p in zip(data[selected_date].times, data[selected_date].non_leveraged_prices)
    if t in data[selected_date].buy_signal_times
]
sell_prices = [
    p
    for t, p in zip(data[selected_date].times, data[selected_date].non_leveraged_prices)
    if t in data[selected_date].sell_signal_times
]


st.text(str(buy_prices))

factors = [s / b * FX for b, s in zip(buy_prices, sell_prices)]
total = math.prod(factors)

st.text(
    f"Total {100 * round(total - 1, 4)}% corresponds to {100 * round(total**251 - 1, 4)}% per year"
)
st.text(str(factors))

st.text(str(sell_prices))


st.text(str(data[selected_date].sell_signal_times))


fig = make_subplots(
    specs=[
        [{"secondary_y": True}],  # row 1
        [{"secondary_y": True}],  # row 2
    ],
    shared_xaxes=True,
    rows=2,
    cols=1,
)
trace_non_leverage = go.Scatter(
    x=data[selected_date].times,
    y=data[selected_date].non_leveraged_prices,
    name="non_leverage",
    # mode='lines+markers'
)
trace_leverage = go.Scatter(
    x=data[selected_date].times,
    y=data[selected_date].leveraged_prices,
    name="leverage",
    mode="lines+markers",
)
# trace_buy_signal=go.Scatter(
#     x=data[selected_date].buy_signal_times,
#     y=[t for t in data[selected_date].buy_signal_times],#data[selected_date].leveraged_prices,
#     mode='lines+markers'
# )
fig.add_trace(trace_non_leverage, row=1, col=1)
fig.add_trace(trace_leverage, secondary_y=True, row=1, col=1)
for t in data[selected_date].buy_signal_times:
    fig.add_vline(x=t, line=dict(color="green", width=1))
for t in data[selected_date].sell_signal_times:
    fig.add_vline(x=t, line=dict(color="red", width=1))

# st.plotly_chart(fig, use_container_width=True)


trace_lev_move = go.Scatter(
    x=data[selected_date].times,
    y=data[selected_date].lev_moves,
    name="lev_move",
    # mode='lines+markers'
)
# trace_leverage = go.Scatter(
#     x=data[selected_date].times,
#     y=data[selected_date].leveraged_prices,
#     name="leverage",
#     mode="lines+markers",
# )
fig.add_trace(trace_lev_move, row=2, col=1)
fig.update_layout(height=900)  # adjust to your desired height

st.plotly_chart(fig, use_container_width=True)

"""
115792089237316195423570985000000000000000000000000000000000000000000000000000000000
115792089237316195423570985008687907853269984665640564039457584007913129639935

"""
