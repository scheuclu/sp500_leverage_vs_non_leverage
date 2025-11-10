import os
import plotly.graph_objects as go
import requests
from models import TradableInstrument, Exchange, WorkingSchedule, Position
from dotenv import load_dotenv
from plotly.subplots import make_subplots
import plotly.io as pio

pio.renderers.default = "browser"
import numpy as np
import pandas as pd
from supabase import Client, create_client
from datetime import datetime, timedelta, date
from pydantic import BaseModel
from plotly.subplots import make_subplots

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

# url = "https://demo.trading212.com/api/v0/equity/portfolio"
# response = requests.get(url, headers=headers)
# positions = [Position(**i) for i in response.json()]


date_str = "2025-10-28T08:47:00.425164+00:00"
dt = datetime.fromisoformat(date_str)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


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


unique_dates = sorted(list(set([datetime.fromisoformat(d["created_at"]).date() for d in all_data])))


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
    if "VUAGl_EQ" in positions and "5LUSl_EQ" in positions:
        data[d].times.append(t)
        data[d].leveraged_prices.append(positions["5LUSl_EQ"].currentPrice)
        data[d].non_leveraged_prices.append(positions["VUAGl_EQ"].currentPrice)


# Compute buy and sell signals
for d, datedata in data.items():
    # buys={'x':[], 'y':[]}
    # sells={'x':[], 'y':[]}
    # lev_moves = []
    current_base_value = datedata.non_leveraged_prices[0]
    lev_at_current_base_value = datedata.leveraged_prices[0]
    current_base_value_since = datedata.times[0]
    for base, lev, dt in zip(
        datedata.non_leveraged_prices, datedata.leveraged_prices, datedata.times
    ):
        # print(dt)
        # if dt.date() != selected_date:
        #     continue
        if base == current_base_value:
            lev_move = lev - lev_at_current_base_value
            if lev_move > 10 and (dt - current_base_value_since).seconds > 300:
                datedata.buy_signal_times.append(dt)
                # buys['x'].append(dt)
                # buys['y'].append(base)
            if lev_move < -10 and (dt - current_base_value_since).seconds > 300:
                # sells['x'].append(dt)
                # sells['y'].append(base)
                datedata.sell_signal_times.append(dt)
        else:
            current_base_value = base
            current_base_value_since = dt
            lev_at_current_base_value = lev
            lev_move = 0
        datedata.lev_moves.append(lev_move)


selected_date = st.selectbox(label="aaa", options=data.keys())


fig = make_subplots(specs=[[{"secondary_y": True}]])
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
fig.add_trace(trace_non_leverage)
fig.add_trace(trace_leverage, secondary_y=True)
for t in data[selected_date].buy_signal_times:
    fig.add_vline(x=t, line=dict(color="green", width=1))
for t in data[selected_date].sell_signal_times:
    fig.add_vline(x=t, line=dict(color="red", width=1))

st.plotly_chart(fig, use_container_width=True)




fig = make_subplots(specs=[[{"secondary_y": True}]])
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
fig.add_trace(trace_lev_move)
st.plotly_chart(fig)