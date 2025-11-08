import os
import plotly.graph_objects as go
import requests
from models import TradableInstrument, Exchange, WorkingSchedule, Position
from dotenv import load_dotenv
from plotly.subplots import make_subplots
import numpy as np
load_dotenv()

TRADING212_KEY = os.environ["TRADING212_KEY"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
headers = {
    "Authorization": TRADING212_KEY,
    "Content-Type": "application/json",
}

url = "https://demo.trading212.com/api/v0/equity/portfolio"
response = requests.get(url, headers=headers)
positions = [Position(**i) for i in response.json()]

from supabase import Client, create_client

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

response = supabase.table("data").select("*").order('created_at').execute()
data = response.data

times = []
leverage = []
non_leverage = []


for d in data:
    print(d)
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


for N in [1,2,3,4]:
    trace=go.Scatter(
        x=diff_non_leverage[:-N],
        y=diff_leverage[N:],
        mode='markers'
    )
    go.Figure(data=trace).show()



trace=go.Scatter(
    x=non_leverage[:-1],
    y=leverage[1:],
    mode='markers'
)
go.Figure(data=trace).show()

# NO it is not
corr = np.corrcoef(diff_leverage, diff_non_leverage)[0, 1]
print(0, corr)
for shift in [1,2,3,4]:
    corr = np.corrcoef(diff_leverage[:-shift], diff_non_leverage[shift:])[0,1]
    print(shift,corr)
for shift in [1,2,3,4]:
    corr = np.corrcoef(diff_leverage[shift:], diff_non_leverage[:-shift])[0,1]
    print(shift,corr)

import pandas as pd
window = 3  # e.g., 3-day rolling correlation
rolling_corr = pd.Series(leverage).rolling(window).corr(pd.Series(non_leverage))



fig = make_subplots(specs=[[{"secondary_y": True}]])
trace_leverage = go.Scatter(x=times, y=leverage, name="5x",mode='markers+lines')
trace_non_leverage = go.Scatter(x=times, y=non_leverage, name="1x",mode='markers+lines')

fig.add_trace(
    trace_non_leverage,
    secondary_y=False,
)

fig.add_trace(
    trace_leverage,
    secondary_y=True,
)

import streamlit as st

st.set_page_config(layout="wide")

st.plotly_chart(fig)
