import os
import plotly.graph_objects as go
import requests
from models import TradableInstrument, Exchange, WorkingSchedule, Position
from dotenv import load_dotenv
from plotly.subplots import make_subplots
import plotly.io as pio

pio.renderers.default = "browser"
import numpy as np

load_dotenv()

import streamlit as st

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

from supabase import Client, create_client
from datetime import datetime, timedelta

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

print(f"Fetched {len(all_data)} rows")
###


times = []
leverage = []
non_leverage = []


for d in all_data:
    # print(d)
    t = d["created_at"]
    times.append(t)
    positions = {
        Position.model_validate_json(p).ticker: Position.model_validate_json(p)
        for p in d["positions"]
    }
    if "VUAGl_EQ" in positions:
        non_leverage.append(positions["VUAGl_EQ"].currentPrice)
    else:
        non_leverage.append(None)

    if "5LUSl_EQ" in positions:
        leverage.append(positions["5LUSl_EQ"].currentPrice)
    else:
        leverage.append(None)

diff_leverage = np.diff(leverage)
diff_non_leverage = np.diff(non_leverage)
diff_leverage = np.insert(diff_leverage, 0, 0)
diff_non_leverage = np.insert(diff_non_leverage, 0, 0)


rel_diff_non_leverage = diff_non_leverage / np.array(non_leverage)
rel_diff_leverage = diff_leverage / np.array(leverage)


NUM_SHARES_NON_LEVERAGE = 100
NUM_SHARES_LEVERAGE = 0

FX = 0.9999
FX = 1.0
from enum import Enum


class State(Enum):
    OUT_OF_LEVERAGE = 0
    IN_LEVERAGE = 1


state = State.OUT_OF_LEVERAGE

date_last_trade = None

for date, price_non_leverage, price_leverage, rel_non_leverage, rel_leverage in zip(
    times, non_leverage, leverage, rel_diff_non_leverage, rel_diff_leverage
):
    # print(NUM_SHARES_NON_LEVERAGE*price_non_leverage+NUM_SHARES_LEVERAGE*price_leverage, NUM_SHARES_NON_LEVERAGE, NUM_SHARES_LEVERAGE)
    if NUM_SHARES_LEVERAGE > 0 and datetime.fromisoformat(
        date
    ) - datetime.fromisoformat(date_last_trade) > timedelta(minutes=10):
        value_leverage = NUM_SHARES_LEVERAGE * price_leverage
        NUM_SHARES_NON_LEVERAGE += value_leverage / price_non_leverage * FX
        NUM_SHARES_LEVERAGE = 0
        print(f"Going out of leverage {date} -> {NUM_SHARES_NON_LEVERAGE}")
        date_last_trade = date
        print(
            NUM_SHARES_NON_LEVERAGE * price_non_leverage
            + NUM_SHARES_LEVERAGE * price_leverage,
            NUM_SHARES_NON_LEVERAGE,
            NUM_SHARES_LEVERAGE,
        )
    else:
        if rel_non_leverage > 0.001 and rel_leverage / rel_non_leverage < 3:
            print(f"Going INTO leverage {date}")
            print(f"  Non-leverage rose {rel_non_leverage}")
            print(f"  leverage rose     {rel_leverage}")

            # print(rel_leverage/rel_non_leverage, rel_non_leverage, rel_leverage)
            MONEY = NUM_SHARES_NON_LEVERAGE * price_non_leverage
            NUM_SHARES_LEVERAGE = MONEY / price_leverage
            NUM_SHARES_NON_LEVERAGE = 0
            date_last_trade = date
            print(
                NUM_SHARES_NON_LEVERAGE * price_non_leverage
                + NUM_SHARES_LEVERAGE * price_leverage,
                NUM_SHARES_NON_LEVERAGE,
                NUM_SHARES_LEVERAGE,
            )
print(NUM_SHARES_NON_LEVERAGE)
# print(date, price_non_leverage, price_leverage, rel_non_leverage, rel_leverage)

######

dts = [datetime.fromisoformat(t) for t in times]
day = [
    datetime.fromisoformat(t).hour * 60 + datetime.fromisoformat(t).minute
    for t in times
]
dates = [datetime.fromisoformat(t).date() for t in times]


selected_date = st.selectbox(options=set(dates), label="date")
st.text(selected_date)

# go.Figure(
#     go.Scatter(
#         x=non_leverage,
#         y=leverage,
#         text=day,
#         marker=dict(color=day),
#         mode='markers'
# )).show()

buys = {"x": [], "y": []}
sells = {"x": [], "y": []}
lev_moves = []
current_base_value = non_leverage[0]
lev_at_current_base_value = leverage[0]
current_base_value_since = dts[0]
for base, lev, dt in zip(non_leverage, leverage, dts):
    # print(dt)
    # if dt.date() != selected_date:
    #     continue
    if base == current_base_value:
        lev_move = lev - lev_at_current_base_value
        if lev_move > 10 and (dt - current_base_value_since).seconds > 300:
            buys["x"].append(dt)
            buys["y"].append(base)
        if lev_move < -10 and (dt - current_base_value_since).seconds > 300:
            sells["x"].append(dt)
            sells["y"].append(base)
    else:
        current_base_value = base
        current_base_value_since = dt
        lev_at_current_base_value = lev
        lev_move = 0
    lev_moves.append(lev_move)

from plotly.subplots import make_subplots

fig = make_subplots(specs=[[{"secondary_y": True}]])
times_selected = [t for t in times if datetime.fromisoformat(t).date() == selected_date]
index_selected = [
    i for i, t in enumerate(times) if datetime.fromisoformat(t).date() == selected_date
]
trace1 = go.Scatter(
    x=times_selected,
    y=[non_leverage for i in index_selected],
    name="non-leveraged",
    # text=day,
    marker=dict(
        color="black",
        showscale=True,
    ),  # 👈 important: makes the colorbar visible
    mode="lines",
)
trace2 = go.Scatter(
    x=times_selected,
    y=[leverage for i in index_selected],
    name="leveraged",
    # text=day,
    marker=dict(color="black"),
    mode="lines",
)
trace3 = go.Scatter(
    x=sells["x"],
    y=sells["y"],
    name="sells",
    # text=day,
    marker=dict(color="red"),
    mode="markers",
)
trace4 = go.Scatter(
    x=buys["x"],
    y=buys["y"],
    name="buys",
    # text=day,
    marker=dict(color="green"),
    mode="markers",
)
fig.add_trace(trace1)
fig.add_trace(trace3)
fig.add_trace(trace4)
# fig.add_trace(trace2, secondary_y=True)
st.plotly_chart(fig)


# 99.4, 3087
# 99.45,3106

"""

99.4, 3083
100.0, 3159

(3159-3083)/(100.0-99.4)
"""
