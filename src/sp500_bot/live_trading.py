from __future__ import annotations

import os
import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from sp500_bot.models import Position, Order, Cash
from sp500_bot.sb import write_positions, write_state
from sp500_bot.t212 import (
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
from sp500_bot.utils import are_positions_tradeable
from sp500_bot.tgbot import send_message

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
STOP_LOSS_THRESHOLD = 0.005  # 0.5% - sell if base drops this much below buy price

BASE_TICKER = Trading212Ticker.SP500_EUR
LEV_TICKER = Trading212Ticker.SP500_EUR_L

# LEV_DIFF_INVEST = 0.0001
# TIME_DIFF_INVEST = timedelta(minutes=1)


class SignalData(BaseModel):
    time_last_base_change: datetime
    base_value_at_last_change: float = Field(default=0.0)
    lev_value_at_last_change: float = Field(default=0.0)


class TraderState(ABC):
    def __init__(self, signal_data: SignalData):
        self.signal_data = signal_data

    @abstractmethod
    def process(
        self,
        base_position: Position,
        lev_position: Position,
        curdatetime: datetime,
    ) -> TraderState:
        pass


class Initializing(TraderState):
    def process(
        self,
        base_position: Position,
        lev_position: Position,
        curdatetime: datetime,
    ) -> ReadyToInvest:
        cancel_open_orders()
        # Sell holdings in base
        time.sleep(10)
        base_position, lev_position = get_current_positions()
        if base_position.quantity > 0.15:
            place_market_order(
                MarketOrder(
                    ticker=BASE_TICKER,
                    quantity=base_position.quantity - 0.1,
                    type=MarketOrderType.SELL,
                )
            )
        send_message("Initialized and ready to invest")
        return ReadyToInvest(
            signal_data=SignalData(
                time_last_base_change=curdatetime,
                base_value_at_last_change=base_position.currentPrice,
                lev_value_at_last_change=lev_position.currentPrice,
            )
        )


class ReadyToInvest(TraderState):
    def process(
        self,
        base_position: Position,
        lev_position: Position,
        curdatetime: datetime,
    ) -> ReadyToInvest | OrderFailed | InvestedInNonLeverage:
        if base_position.currentPrice != self.signal_data.base_value_at_last_change:
            # Base asset price change - update signal data and stay in same state
            logging.info("Base price updated")
            return ReadyToInvest(
                signal_data=SignalData(
                    time_last_base_change=curdatetime,
                    base_value_at_last_change=base_position.currentPrice,
                    lev_value_at_last_change=lev_position.currentPrice,
                )
            )

        lev_diff_rel = (
            lev_position.currentPrice - self.signal_data.lev_value_at_last_change
        ) / self.signal_data.lev_value_at_last_change
        logging.info(
            f"{round(lev_diff_rel, 4)} | {curdatetime - self.signal_data.time_last_base_change}"
        )

        if (
            lev_diff_rel > LEV_DIFF_INVEST
            and curdatetime - self.signal_data.time_last_base_change > TIME_DIFF_INVEST
        ):
            # Make Investment
            cash: Cash = fetch_account_cash()
            available = cash.availableToTrade
            current_price = base_position.currentPrice
            lev_current_price = lev_position.currentPrice
            logging.info(f"Cash available: {available}, current price: {current_price}")
            if available is None or current_price is None or lev_current_price is None or available <= 0:
                logging.warning(
                    f"Cannot place order: availableToTrade={available}, currentPrice={current_price}"
                )
                return self  # Stay in ReadyToInvest state
            assert available is not None and current_price is not None and lev_current_price is not None
            quantity: float = available / current_price
            if quantity * 0.9 < 0.01:  # Minimum order quantity check
                logging.warning(
                    f"Insufficient cash to place order. Available: {available}, quantity would be: {quantity * 0.9}"
                )
                return self  # Stay in ReadyToInvest state
            send_message(
                f"Placing an order for {quantity} at {current_price * 1.0001}. Lev went up by factor {lev_diff_rel}"
            )
            try:
                buy_order = place_limit_order(
                    LimitOrder(
                        ticker=BASE_TICKER,
                        quantity=quantity * 0.9,  # TODO
                        limit_price=current_price * (1 + LEV_DIFF_INVEST / 8),
                        type=LimitOrderType.BUY,
                    )
                )
                filled = wait_for_order_or_cancel(id=buy_order.id, max_wait_seconds=3 * 60)
                if not filled:
                    send_message("Buy order was not filled")
                    return OrderFailed(signal_data=self.signal_data)
                else:
                    send_message("Buy order succeeded")
                    # Record the buy price as new baseline for sell decision
                    return InvestedInNonLeverage(
                        signal_data=SignalData(
                            time_last_base_change=curdatetime,
                            base_value_at_last_change=current_price,
                            lev_value_at_last_change=lev_current_price,
                        )
                    )
            except Exception as e:
                send_message(f"Error placing buy order: {str(e)}")
                return OrderFailed(signal_data=self.signal_data)

        # No action needed, stay in same state
        return self


class InvestedInNonLeverage(TraderState):
    def process(
        self,
        base_position: Position,
        lev_position: Position,
        curdatetime: datetime,
    ) -> InvestedInNonLeverage | Initializing | OrderFailed:
        stop_loss_price = self.signal_data.base_value_at_last_change * (1 - STOP_LOSS_THRESHOLD)

        if base_position.currentPrice > self.signal_data.base_value_at_last_change:
            # Base price moved UP - sell and take profit
            send_message("Base price increased, placing sell order")
            return self._sell_position(base_position, lev_position, curdatetime)

        elif base_position.currentPrice < stop_loss_price:
            # Stop-loss triggered - cut losses
            send_message(
                f"Stop-loss triggered! Price {base_position.currentPrice} < {stop_loss_price:.2f}"
            )
            return self._sell_position(base_position, lev_position, curdatetime)

        # No action needed, stay in same state
        return self

    def _sell_position(
        self,
        base_position: Position,
        lev_position: Position,
        curdatetime: datetime,
    ) -> Initializing | OrderFailed:
        """Helper to sell position and transition state."""
        time.sleep(2)  # because we may just have made a buy order
        # Re-fetch positions to get accurate quantity after buy
        base_position, lev_position = get_current_positions()
        try:
            order: Order = place_limit_order(
                LimitOrder(
                    ticker=BASE_TICKER,
                    quantity=base_position.quantity - 0.1,  # Don't sell everything
                    limit_price=base_position.currentPrice * (1 - LEV_DIFF_INVEST / 8),
                    type=LimitOrderType.SELL,
                )
            )
            filled = wait_for_order_or_cancel(id=order.id, max_wait_seconds=3 * 60)
            if not filled:
                send_message("Sell order failed")
                return OrderFailed(signal_data=self.signal_data)
            else:
                send_message("Sell order succeeded")
                return Initializing(
                    signal_data=SignalData(
                        time_last_base_change=curdatetime,
                        base_value_at_last_change=base_position.currentPrice,
                        lev_value_at_last_change=lev_position.currentPrice,
                    )
                )
        except Exception as e:
            send_message(f"Error placing order: {str(e)}")
            return OrderFailed(signal_data=self.signal_data)


class OrderFailed(TraderState):
    def process(
        self,
        base_position: Position,
        lev_position: Position,
        curdatetime: datetime,
    ) -> Initializing:
        send_message("Landed in order failed. Will Re-initialize")
        return Initializing(signal_data=self.signal_data)


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
        p.instrument.ticker: p
        for p in fetch_positions()
        if p.instrument and p.instrument.ticker and p.instrument.ticker in ticker_values
    }
    base_position: Position = positions[BASE_TICKER.value]
    lev_position: Position = positions[LEV_TICKER.value]
    return base_position, lev_position


def main():
    instruments = fetch_instruments()
    exchanges = fetch_exchanges()

    INTERVAL = 20  # seconds
    next_run = time.time()

    trader_state: TraderState = Initializing(
        signal_data=SignalData(
            time_last_base_change=datetime.now(),
            base_value_at_last_change=0.0,
            lev_value_at_last_change=0.0,
        )
    )

    while True:
        logging.info(f"{trader_state.__class__.__name__}")

        base_position, lev_position = get_current_positions()

        all_open: bool = are_positions_tradeable(
            exchanges, instruments, [base_position, lev_position]
        )
        if not all_open:
            logging.info("Not all open")
            time.sleep(300)
            continue

        write_positions([base_position, lev_position])
        curdatetime = datetime.now()

        trader_state = trader_state.process(base_position, lev_position, curdatetime)

        # Write current state to Supabase
        write_state(
            state_name=trader_state.__class__.__name__,
            time_last_base_change=trader_state.signal_data.time_last_base_change,
            base_value_at_last_change=trader_state.signal_data.base_value_at_last_change,
            lev_value_at_last_change=trader_state.signal_data.lev_value_at_last_change,
        )

        # Schedule the next run based on absolute time
        next_run += INTERVAL
        sleep_time = next_run - time.time()
        logging.info(f"Sleeping for {round(sleep_time, 4)} s.")
        if sleep_time > 0:
            time.sleep(sleep_time)
        else:  # If we're running behind schedule, skip missed intervals
            next_run = time.time()


if __name__ == "__main__":
    main()
