import os
import logging
from more_itertools.recipes import quantify

from models import TradableInstrument, Position, Exchange, Order, Cash
from dotenv import load_dotenv
from sb import write_positions, APIResponse

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
    cancel_open_orders,
    place_market_order,
    MarketOrder,
    MarketOrderType,
)
from utils import are_positions_tradeable
import time
from tgbot import send_message
from enum import Enum
from datetime import datetime, timedelta
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO,
    format="{levelname}:{name}:{filename}:{lineno}: {message}",
    style="{",
    force=True,
)

load_dotenv()

TRADING212_KEY = os.environ["TRADING212_KEY"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]

LEV_DIFF_INVEST = 0.004
TIME_DIFF_INVEST = timedelta(minutes=2)

BASE_TICKER = Trading212Ticker.SP500_EUR
LEV_TICKER = Trading212Ticker.SP500_EUR_L

# LEV_DIFF_INVEST = 0.0001
# TIME_DIFF_INVEST = timedelta(minutes=1)

buy_order: Order = Order()


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


class SignalData(BaseModel):
    time_last_base_change: datetime
    base_value_at_last_change: float = Field(default=0.0, description="TODO")
    lev_value_at_last_change: float = Field(default=0.0, description="TODO")


def wait_for_order_or_cancel(id: int, max_wait_seconds: int) -> bool:
    order_filled = has_order_been_filled(id)
    if order_filled:
        return True
    start = time.time()
    while not order_filled:
        logging.info("Order is still open")
        order_filled = has_order_been_filled(id)
        if order_filled:
            return True
        time.sleep(1.1)  # time limit
        if time.time() - start > max_wait_seconds:
            success = cancel_order_by_id(id)
            assert success
            return False
    return False


def get_current_positions() -> tuple[Position, Position]:
    # Fetch positions if exchanges are open
    ticker_values: list[str] = [i.value for i in Trading212Ticker.__members__.values()]
    positions: dict[Trading212Ticker, Position] = {
        p.ticker: p for p in fetch_positions() if p.ticker in ticker_values
    }
    base_position: Position = positions[BASE_TICKER.value]
    lev_position: Position = positions[LEV_TICKER.value]
    return base_position, lev_position


if __name__ == "__main__":
    instruments: list[TradableInstrument] = fetch_instruments()
    exchanges: list[Exchange] = fetch_exchanges()

    INTERVAL = 20  # seconds

    next_run = time.time()

    trader_state = State.INITIALIZING
    signal_data: SignalData = SignalData(
        time_last_base_change=datetime.now(),
        base_value_at_last_change=0.0,
        lev_value_at_last_change=0.0,
    )

    while True:
        start = time.time()
        logging.info(f"{trader_state}")

        base_position, lev_position = get_current_positions()

        all_open: bool = are_positions_tradeable(
            exchanges, instruments, [base_position, lev_position]
        )
        if not all_open:
            logging.info("Not all open")
            time.sleep(300)
            continue
        response: APIResponse = write_positions([base_position, lev_position])
        curdatetime = datetime.now()

        match trader_state:
            case State.INITIALIZING:
                cancel_open_orders()
                # Sell holdings in base
                time.sleep(10)
                base_position, lev_position = get_current_positions()
                if base_position.quantity > 0.15:
                    order: Order = place_market_order(
                        MarketOrder(
                            ticker=BASE_TICKER,
                            quantity=base_position.quantity - 0.1,
                            type=MarketOrderType.SELL,
                        )
                    )
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
                    logging.info("Base price updated")
                else:
                    lev_diff_rel = (
                        lev_position.currentPrice - signal_data.lev_value_at_last_change
                    ) / signal_data.lev_value_at_last_change
                    logging.info(
                        f"{round(lev_diff_rel, 4)} | {curdatetime - signal_data.time_last_base_change}"
                    )
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
                        try:
                            buy_order = place_limit_order(
                                LimitOrder(
                                    ticker=BASE_TICKER,
                                    quantity=quantity * 0.9,  # TODO
                                    limit_price=base_position.currentPrice
                                    * (1 + LEV_DIFF_INVEST / 8),
                                    type=LimitOrderType.BUY,
                                )
                            )
                            # order: Order = place_market_order(
                            #     MarketOrder(
                            #         ticker=BASE_TICKER,
                            #         quantity=quantity * 0.7,  # TODO
                            #         type=MarketOrderType.BUY,
                            #     )
                            # )
                            ID = buy_order.id
                            filled = wait_for_order_or_cancel(
                                id=buy_order.id, max_wait_seconds=3 * 60
                            )
                            if not filled:
                                trader_state = trader_state.ORDER_FAILED
                                send_message("Buy order was not filled")
                            else:
                                trader_state = State.INVESTED_IN_NON_LEVERAGE  # TODO
                                send_message("Buy order succeeded")
                        except Exception as e:
                            send_message(f"Erro placing buy order: {str(e)}")
                            trader_state = State.ORDER_FAILED

            case State.INVESTED_IN_NON_LEVERAGE:
                # TODO wait for base price to increase
                if base_position.currentPrice != signal_data.base_value_at_last_change:
                    signal_data.time_last_base_change = curdatetime
                    signal_data.base_value_at_last_change = base_position.currentPrice
                    signal_data.lev_value_at_last_change = lev_position.currentPrice
                    send_message("Placing sell order")
                    time.sleep(2)  # because we may just have made a buy order
                    try:
                        order: Order = place_limit_order(
                            LimitOrder(
                                ticker=BASE_TICKER,
                                quantity=base_position.quantity
                                - 0.1,  # Dont sell everything, otherwise I cant query the price(may no longer be true)
                                limit_price=base_position.currentPrice
                                * (1 - LEV_DIFF_INVEST / 8),  # TODO
                                # limit_price=buy_order.limitPrice,
                                type=LimitOrderType.SELL,
                            )
                        )
                        # order: Order = place_market_order(
                        #     MarketOrder(
                        #         ticker=BASE_TICKER,
                        #         quantity=base_position.quantity
                        #         - 0.1,  # Dont sell everything, otherwise I cant query the price(may no longer be true)
                        #         type=MarketOrderType.SELL,
                        #     )
                        # )
                        ID = order.id

                        filled = wait_for_order_or_cancel(
                            id=order.id, max_wait_seconds=3 * 60
                        )
                        if not filled:
                            trader_state = trader_state.ORDER_FAILED
                            send_message("Sell order failed")
                        else:
                            trader_state = State.INITIALIZING  # TODO
                            send_message("Sell order succeeded")
                    except Exception as e:
                        send_message(f"Erro placing order: {str(e)}")
                        trader_state = State.ORDER_FAILED

            case State.ORDER_FAILED:
                send_message("Landed in order failed. Will Re-initialize")
                trader_state = trader_state.INITIALIZING
            case _:
                raise "Unknown state"

        # Schedule the next run based on absolute time
        next_run += INTERVAL
        sleep_time = next_run - time.time()
        logging.info(f"Sleeping for {round(sleep_time, 4)} s.")
        if sleep_time > 0:
            time.sleep(sleep_time)
        else:  # If we’re running behind schedule, skip missed intervals
            next_run = time.time()
