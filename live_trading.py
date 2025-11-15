import os

from more_itertools.recipes import quantify

from models import TradableInstrument, Position, Exchange, Order, Cash
from dotenv import load_dotenv

from t212 import (
    fetch_instruments,
    Trading212Ticker,
    fetch_positions,
    fetch_exchanges,
    place_limit_order,
    LimitOrder,
    LimitOrderType,
    has_order_been_filled,
    cancel_order_by_id,
    fetch_account_cash,
)
from utils import are_positions_tradeable
import time
from tgbot import send_message
from enum import Enum
from datetime import datetime, timedelta

load_dotenv()

TRADING212_KEY = os.environ["TRADING212_KEY"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]

LEV_DIFF_INVEST = 1.0005
TIME_DIFF_INVEST = timedelta(minutes=10)


class State(Enum):
    INVESTED_IN_LEVERAGE = 0
    INVESTED_IN_NON_LEVERAGE = 1
    READY_TO_INVEST = 2
    INITIALIZING = 99
    ORDER_FAILED = 404

    def __init__(
        self, code, num_shares_leverage=0, num_shares_non_leverage=0, cash=10000
    ):
        self.code = code
        self.num_shares_leverage = num_shares_leverage
        self.num_shares_non_leverage = num_shares_non_leverage
        self.cash = cash


from pydantic import BaseModel, Field


class SignalData(BaseModel):
    time_last_base_change: datetime
    base_value_at_last_change: float
    lev_value_at_last_change: float


def wait_for_order_or_cancel(id: int, max_wait_seconds: int) -> bool:
    order_filled = has_order_been_filled(id)
    start = time.time()
    while not order_filled:
        print("Order is still open")
        order_filled = has_order_been_filled(id)
        time.sleep(1.1)  # time limit
        if time.time() - start > max_wait_seconds:
            success = cancel_order_by_id(id)
            assert success
            return False
    return True


if __name__ == "__main__":
    instruments: list[TradableInstrument] = fetch_instruments()
    exchanges: list[Exchange] = fetch_exchanges()

    INTERVAL = 30  # seconds

    next_run = time.time()

    trader_state = State.INITIALIZING
    signal_data: SignalData = SignalData(
        time_last_base_change=datetime.now(),
        base_value_at_last_change=0.0,
        lev_value_at_last_change=0.0,
    )

    while True:
        start = time.time()

        # Fetch positions if exchanges are open
        ticker_values: list[str] = [
            i.value for i in Trading212Ticker.__members__.values()
        ]
        positions: dict[Trading212Ticker, Position] = {
            p.ticker: p for p in fetch_positions() if p.ticker in ticker_values
        }
        base_position: Position = positions[Trading212Ticker.SP500_ACC.value]
        lev_position: Position = positions[Trading212Ticker.SP500_5L.value]
        all_open: bool = are_positions_tradeable(
            exchanges, instruments, list(positions.values())
        )
        if not all_open:
            print("Not all open")
            time.sleep(300)
            continue
        curdatetime = datetime.now()

        match trader_state:
            case State.INITIALIZING:
                signal_data.time_last_base_change = curdatetime
                signal_data.base_value_at_last_change = base_position.currentPrice
                signal_data.lev_value_at_last_change = lev_position.currentPrice
                trader_state = State.READY_TO_INVEST
                send_message("Initialized and ready to invest")
            case State.READY_TO_INVEST:
                if (
                    base_position.currentPrice != signal_data.base_value_at_last_change
                ):  # Base asset price change
                    signal_data.time_last_base_change = curdatetime
                    signal_data.base_value_at_last_change = base_position.currentPrice
                    signal_data.lev_value_at_last_change = lev_position.currentPrice
                else:
                    lev_diff_rel = (
                        lev_position.currentPrice - signal_data.lev_value_at_last_change
                    ) / signal_data.lev_value_at_last_change
                    if (
                        lev_diff_rel > LEV_DIFF_INVEST
                        and curdatetime - signal_data.time_last_base_change
                        > TIME_DIFF_INVEST
                    ):
                        # Make Investment
                        cash: Cash = fetch_account_cash()
                        quantity: float = cash.free / base_position.currentPrice
                        send_message(
                            f"Placing an order for {quantity} at {base_position.currentPrice * 1.0001}. Lev went up by factor {lev_diff_rel}"
                        )
                        order: Order = place_limit_order(
                            LimitOrder(
                                ticker=Trading212Ticker.SP500_ACC,
                                quantity=quantity * 0.99,  # TODO
                                limit_price=base_position.currentPrice * 1.0001,  # TODO
                                type=LimitOrderType.BUY,
                            )
                        )
                        ID = order.id
                        filled = wait_for_order_or_cancel(
                            id=order.id, max_wait_seconds=3 * 60
                        )
                        if not filled:
                            trader_state = trader_state.ORDER_FAILED
                            send_message("Buy order failed")
                        else:
                            trader_state = State.INVESTED_IN_NON_LEVERAGE  # TODO
                            send_message("Buy order succeeded")
            case State.INVESTED_IN_NON_LEVERAGE:
                order: Order = place_limit_order(
                    LimitOrder(
                        ticker=Trading212Ticker.SP500_ACC,
                        quantity=base_position.quantity-0.1,  # Dont sell everything, otherwise I cant query the price(may no longer be true)
                        limit_price=base_position.currentPrice * 0.9999,  # TODO
                        type=LimitOrderType.SELL,
                    )
                )
                ID = order.id
                filled = wait_for_order_or_cancel(id=order.id, max_wait_seconds=3 * 60)
            case _:
                raise "Unknown state"
