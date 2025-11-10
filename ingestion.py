import os

import requests
from models import (
    TradableInstrument,
    Exchange,
    WorkingSchedule,
    Position,
    TimeEvent,
    Type3,
)
from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()
import time

TRADING212_KEY = os.environ["TRADING212_KEY"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]


import datetime

# class TzInfo(datetime.tzinfo):
#     def __init__(self, offset_hours: int):
#         self.offset = datetime.timedelta(hours=offset_hours)
#     def utcoffset(self, dt):
#         return self.offset
#     def tzname(self, dt):
#         return f"TzInfo({int(self.offset.total_seconds() / 3600)})"
#     def dst(self, dt):
#         return datetime.timedelta(0)


url = "https://demo.trading212.com/api/v0/equity/metadata/instruments"
headers = {
    "Authorization": TRADING212_KEY,
    "Content-Type": "application/json",
}
response = requests.get(url, headers=headers)
response.raise_for_status()
instruments = {d["ticker"]: TradableInstrument(**d) for d in response.json()}

# instruments["VUAGl_EQ"].workingScheduleId
# instruments["5SPYl_EQ"].workingScheduleId
# # AAPL_US_EQ


# timeEvents = workingSchedules[70].timeEvents


def is_exchange_open(timeEvents: list[TimeEvent]):
    now = datetime.datetime.now(datetime.timezone.utc)
    last_event = [t for t in timeEvents if t.date < now][-1]
    return last_event.type == Type3.OPEN


url = "https://demo.trading212.com/api/v0/equity/metadata/exchanges"
response = requests.get(url, headers=headers)
response.raise_for_status()
exchanges = [Exchange(**d) for d in response.json()]

workingSchedules: dict[int, WorkingSchedule] = {}
for exchange in exchanges:
    for w in exchange.workingSchedules:
        id = w.id
        workingSchedules[id] = w


INTERVAL = 30  # seconds

next_run = time.time()

while True:
    start = time.time()

    url = "https://demo.trading212.com/api/v0/equity/portfolio"
    response = requests.get(url, headers=headers)
    positions = [Position(**i) for i in response.json()]

    working_schedule_ids = [instruments[p.ticker].workingScheduleId for p in positions]
    ws = [workingSchedules[id] for id in working_schedule_ids]
    all_open = all([is_exchange_open(w.timeEvents) for w in ws])

    if all_open:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        response = (
            supabase.table("data")
            .insert({"positions": [p.model_dump_json() for p in positions]})
            .execute()
        )
        print(response)
    else:
        print("Not all open")
        time.sleep(300)  # Wait 5 minutes to check for open exchange

    # Schedule the next run based on absolute time
    next_run += INTERVAL
    sleep_time = next_run - time.time()

    if sleep_time > 0:
        time.sleep(sleep_time)
    else:
        # If we’re running behind schedule, skip missed intervals
        next_run = time.time()
