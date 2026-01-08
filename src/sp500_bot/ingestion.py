import os
import time

from sp500_bot.models import (
    TradableInstrument,
    Exchange,
    Position,
)
from dotenv import load_dotenv

from postgrest import APIResponse

from sp500_bot.t212 import fetch_instruments, fetch_exchanges, fetch_positions, Trading212Ticker
from sp500_bot.sb import write_positions
from sp500_bot.utils import are_positions_tradeable

load_dotenv()

TRADING212_KEY = os.environ["TRADING212_KEY"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]


headers = {
    "Authorization": TRADING212_KEY,
    "Content-Type": "application/json",
}


def main():
    instruments: list[TradableInstrument] = fetch_instruments()
    exchanges: list[Exchange] = fetch_exchanges()

    INTERVAL = 20  # seconds

    next_run = time.time()

    while True:
        start = time.time()

        ticker_values: list[str] = [i.value for i in Trading212Ticker.__members__.values()]
        positions: list[Position] = [
            p for p in fetch_positions() if p.ticker in ticker_values
        ]

        # Wait 5 min if markets are not open
        all_open: bool = are_positions_tradeable(exchanges, instruments, positions)
        if not all_open:
            print("Not all open")
            time.sleep(300)
            continue

        response: APIResponse = write_positions(positions)

        # Schedule the next run based on absolute time
        next_run += INTERVAL
        sleep_time = next_run - time.time()
        if sleep_time > 0:
            time.sleep(sleep_time)
        else:  # If we're running behind schedule, skip missed intervals
            next_run = time.time()


if __name__ == "__main__":
    main()
