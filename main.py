import os

import requests
from models import TradableInstrument, Exchange, WorkingSchedule, Position
from dotenv import load_dotenv

load_dotenv()

TRADING212_KEY = os.environ["TRADING212_KEY"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]

url = "https://demo.trading212.com/api/v0/equity/metadata/instruments"
headers = {
    "Authorization": TRADING212_KEY,
    "Content-Type": "application/json",
}
response = requests.get(url, headers=headers)
instruments = {d["ticker"]: TradableInstrument(**d) for d in response.json()}

# instruments["VUAGl_EQ"].workingScheduleId
# instruments["5SPYl_EQ"].workingScheduleId
# # AAPL_US_EQ


url = "https://demo.trading212.com/api/v0/equity/metadata/exchanges"
response = requests.get(url, headers=headers)
exchanges = [Exchange(**d) for d in response.json()]

workingSchedules: dict[int, WorkingSchedule] = {}
for exchange in exchanges:
    for w in exchange.workingSchedules:
        id = w.id
        workingSchedules[id] = w


url = "https://demo.trading212.com/api/v0/equity/portfolio"
response = requests.get(url, headers=headers)
positions = [Position(**i) for i in response.json()]

from supabase import Client, create_client

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

response = (
    supabase.table("data")
    .insert({"positions": [p.model_dump_json() for p in positions]})
    .execute()
)
print(response)
