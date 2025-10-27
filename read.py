import os
import plotly.graph_objects as go
import requests
from models import TradableInstrument, Exchange, WorkingSchedule, Position
from dotenv import load_dotenv
from plotly.subplots import make_subplots

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

response = supabase.table("data").select("*").execute()
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


fig = make_subplots(specs=[[{"secondary_y": True}]])
trace_leverage = go.Scatter(x=times, y=leverage, name="5x")
trace_non_leverage = go.Scatter(x=times, y=non_leverage, name="1x")

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
