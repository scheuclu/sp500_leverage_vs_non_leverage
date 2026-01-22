from __future__ import annotations

import os
import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from sp500_bot.models import Position, AccountSummary
from sp500_bot.sb import write_positions, write_state
from sp500_bot.t212 import (
    fetch_instruments,
    Trading212Ticker,
    fetch_positions,
    fetch_exchanges,
    place_market_order,
    MarketOrder,
    MarketOrderType,
    fetch_account_summary,
    cancel_open_orders,
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
    position_entry_price: float = Field(default=0.0)  # Entry price for P&L tracking


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
    """
    Initialize the trading bot. Determines current holdings and transitions to the
    appropriate state (HoldingLeveraged or HoldingNonLeveraged).

    Strategy: Always be invested - either in leveraged (3x) or non-leveraged (1x).
    No cash holding except during swaps.
    """

    def process(
        self,
        base_position: Position,
        lev_position: Position,
        curdatetime: datetime,
    ) -> HoldingLeveraged | HoldingNonLeveraged | Initializing:
        cancel_open_orders()
        time.sleep(10)
        base_position, lev_position = get_current_positions()
        account_summary: AccountSummary = fetch_account_summary()

        # Determine current holdings
        base_value = base_position.quantity * base_position.currentPrice
        lev_value = lev_position.quantity * lev_position.currentPrice
        cash_available = account_summary.cash.availableToTrade

        logging.info(
            f"Init: base_value={base_value:.2f}, lev_value={lev_value:.2f}, cash={cash_available:.2f}"
        )

        # If we have significant non-leveraged holdings, stay in that position
        if base_value > lev_value and base_value > cash_available:
            send_message(f"Initialized: Already holding non-leveraged (value={base_value:.2f})")
            return HoldingNonLeveraged(
                signal_data=SignalData(
                    time_last_base_change=curdatetime,
                    base_value_at_last_change=base_position.currentPrice,
                    lev_value_at_last_change=lev_position.currentPrice,
                    position_entry_price=base_position.currentPrice,  # Use current as entry
                )
            )

        # If we have significant leveraged holdings, stay in that position
        if lev_value > base_value and lev_value > cash_available:
            send_message(f"Initialized: Already holding leveraged (value={lev_value:.2f})")
            return HoldingLeveraged(
                signal_data=SignalData(
                    time_last_base_change=curdatetime,
                    base_value_at_last_change=base_position.currentPrice,
                    lev_value_at_last_change=lev_position.currentPrice,
                    position_entry_price=lev_position.currentPrice,
                )
            )

        # If we have cash, buy leveraged (default holding) using market order
        if cash_available > 10:
            send_message(f"Initialized: Buying leveraged with cash={cash_available:.2f}")
            quantity = cash_available / lev_position.currentPrice
            try:
                place_market_order(
                    MarketOrder(
                        ticker=LEV_TICKER,
                        quantity=quantity * 0.9,
                        type=MarketOrderType.BUY,
                    )
                )
                send_message("Market buy order placed for leveraged")
            except Exception as e:
                send_message(f"Init buy error: {str(e)}, staying in Initializing")
                return Initializing(signal_data=self.signal_data)

            # Wait and verify the order was filled by checking positions
            time.sleep(5)
            base_position, lev_position = get_current_positions()
            new_lev_value = lev_position.quantity * lev_position.currentPrice

            if new_lev_value <= lev_value + 5:  # Order didn't fill (no significant increase)
                send_message("Init buy order not filled, staying in Initializing")
                return Initializing(signal_data=self.signal_data)

            send_message("Initialized: Holding leveraged (bought successfully)")
            return HoldingLeveraged(
                signal_data=SignalData(
                    time_last_base_change=curdatetime,
                    base_value_at_last_change=base_position.currentPrice,
                    lev_value_at_last_change=lev_position.currentPrice,
                    position_entry_price=lev_position.currentPrice,
                )
            )

        # No cash and no significant holdings - stay in Initializing
        send_message("No significant holdings or cash, staying in Initializing")
        return Initializing(signal_data=self.signal_data)


class HoldingLeveraged(TraderState):
    """
    Holding the leveraged (3x) ETF. This is the default/primary holding.

    Monitors for leveraged overperformance (positive divergence).
    When leveraged diverges UP significantly (> LEV_DIFF_INVEST for > TIME_DIFF_INVEST),
    swap to non-leveraged to hedge against mean reversion.

    Transition triggers:
    - Leveraged diverges UP > threshold → SWAP to HoldingNonLeveraged
    - Base price changes → update reference, stay in state
    """

    def process(
        self,
        base_position: Position,
        lev_position: Position,
        curdatetime: datetime,
    ) -> HoldingLeveraged | HoldingNonLeveraged:
        # If base price changed, update reference and stay
        if base_position.currentPrice != self.signal_data.base_value_at_last_change:
            logging.info("Base price updated - resetting divergence reference")
            return HoldingLeveraged(
                signal_data=SignalData(
                    time_last_base_change=curdatetime,
                    base_value_at_last_change=base_position.currentPrice,
                    lev_value_at_last_change=lev_position.currentPrice,
                    position_entry_price=self.signal_data.position_entry_price,
                )
            )

        # Calculate divergence: how much has leveraged moved since last base update
        lev_diff_rel = (
            lev_position.currentPrice - self.signal_data.lev_value_at_last_change
        ) / self.signal_data.lev_value_at_last_change
        time_since_base_change = curdatetime - self.signal_data.time_last_base_change
        logging.info(f"Lev divergence: {round(lev_diff_rel, 4)} | Time: {time_since_base_change}")

        # If leveraged diverged UP significantly for long enough → swap to non-leveraged
        if lev_diff_rel > LEV_DIFF_INVEST and time_since_base_change > TIME_DIFF_INVEST:
            send_message(
                f"Leveraged overperforming (+{lev_diff_rel:.4f}). Swapping to non-leveraged."
            )
            return self._swap_to_non_leveraged(base_position, lev_position, curdatetime)

        # No action needed, stay in same state
        return self

    def _swap_to_non_leveraged(
        self,
        base_position: Position,
        lev_position: Position,
        curdatetime: datetime,
    ) -> HoldingNonLeveraged | HoldingLeveraged:
        """Sell leveraged, buy non-leveraged using market orders."""
        time.sleep(2)
        base_position, lev_position = get_current_positions()
        initial_lev_value = lev_position.quantity * lev_position.currentPrice

        # Step 1: Sell leveraged
        try:
            if lev_position.quantity > 0.01:
                place_market_order(
                    MarketOrder(
                        ticker=LEV_TICKER,
                        quantity=lev_position.quantity - 0.01,
                        type=MarketOrderType.SELL,
                    )
                )
                send_message("Market sell order placed for leveraged")
        except Exception as e:
            send_message(f"Error selling leveraged: {str(e)}, staying in HoldingLeveraged")
            return HoldingLeveraged(signal_data=self.signal_data)

        # Wait and verify sell completed
        time.sleep(5)
        base_position, lev_position = get_current_positions()
        new_lev_value = lev_position.quantity * lev_position.currentPrice

        if new_lev_value >= initial_lev_value - 5:  # Sell didn't go through
            send_message("Sell leveraged order not filled, staying in HoldingLeveraged")
            return HoldingLeveraged(signal_data=self.signal_data)

        send_message("Sold leveraged successfully")

        # Step 2: Buy non-leveraged
        account_summary = fetch_account_summary()
        if account_summary.cash.availableToTrade < 10:
            send_message("No cash available after sell, staying in HoldingLeveraged")
            return HoldingLeveraged(signal_data=self.signal_data)

        base_position, lev_position = get_current_positions()
        initial_base_value = base_position.quantity * base_position.currentPrice
        quantity = account_summary.cash.availableToTrade / base_position.currentPrice

        try:
            place_market_order(
                MarketOrder(
                    ticker=BASE_TICKER,
                    quantity=quantity * 0.9,
                    type=MarketOrderType.BUY,
                )
            )
            send_message("Market buy order placed for non-leveraged")
        except Exception as e:
            send_message(f"Error buying non-leveraged: {str(e)}, staying in HoldingLeveraged")
            return HoldingLeveraged(signal_data=self.signal_data)

        # Wait and verify buy completed
        time.sleep(5)
        base_position, lev_position = get_current_positions()
        new_base_value = base_position.quantity * base_position.currentPrice

        if new_base_value <= initial_base_value + 5:  # Buy didn't go through
            send_message("Buy non-leveraged order not filled, staying in HoldingLeveraged")
            return HoldingLeveraged(signal_data=self.signal_data)

        send_message("Bought non-leveraged successfully. Swap complete.")
        return HoldingNonLeveraged(
            signal_data=SignalData(
                time_last_base_change=curdatetime,
                base_value_at_last_change=base_position.currentPrice,
                lev_value_at_last_change=lev_position.currentPrice,
                position_entry_price=base_position.currentPrice,
            )
        )


class HoldingNonLeveraged(TraderState):
    """
    Holding the non-leveraged (1x) ETF. This is a temporary hedge position.

    Monitors for:
    1. Profit opportunity: base price went UP from entry → swap back to leveraged
    2. Leveraged underperformance: negative divergence → swap back to leveraged
    3. Stop-loss: base dropped significantly → swap back to leveraged

    Transition triggers:
    - Base price > entry price → SWAP to HoldingLeveraged (take profit)
    - Leveraged diverges DOWN < -threshold → SWAP to HoldingLeveraged (capture recovery)
    - Stop-loss triggered → SWAP to HoldingLeveraged
    - Base price changed (not up from entry) → update divergence reference, stay
    """

    def process(
        self,
        base_position: Position,
        lev_position: Position,
        curdatetime: datetime,
    ) -> HoldingNonLeveraged | HoldingLeveraged:
        entry_price = self.signal_data.position_entry_price
        stop_loss_price = entry_price * (1 - STOP_LOSS_THRESHOLD)

        # Check for profit: base went UP from entry
        if base_position.currentPrice > entry_price:
            send_message(
                f"Base price increased ({base_position.currentPrice:.2f} > {entry_price:.2f}). Taking profit, swapping to leveraged."
            )
            return self._swap_to_leveraged(base_position, lev_position, curdatetime)

        # Check for stop-loss
        if base_position.currentPrice < stop_loss_price:
            send_message(
                f"Stop-loss triggered! Price {base_position.currentPrice:.2f} < {stop_loss_price:.2f}. Swapping to leveraged."
            )
            return self._swap_to_leveraged(base_position, lev_position, curdatetime)

        # If base price changed (but not up from entry), update divergence reference
        if base_position.currentPrice != self.signal_data.base_value_at_last_change:
            logging.info("Base price updated - resetting divergence reference (staying in non-lev)")
            return HoldingNonLeveraged(
                signal_data=SignalData(
                    time_last_base_change=curdatetime,
                    base_value_at_last_change=base_position.currentPrice,
                    lev_value_at_last_change=lev_position.currentPrice,
                    position_entry_price=entry_price,  # Keep original entry price
                )
            )

        # Calculate divergence for negative divergence check
        lev_diff_rel = (
            lev_position.currentPrice - self.signal_data.lev_value_at_last_change
        ) / self.signal_data.lev_value_at_last_change
        time_since_base_change = curdatetime - self.signal_data.time_last_base_change
        logging.info(f"Lev divergence: {round(lev_diff_rel, 4)} | Time: {time_since_base_change}")

        # Check for negative divergence: leveraged underperforming → swap back to capture recovery
        if lev_diff_rel < -LEV_DIFF_INVEST and time_since_base_change > TIME_DIFF_INVEST:
            send_message(
                f"Leveraged underperforming ({lev_diff_rel:.4f}). Swapping to leveraged to capture recovery."
            )
            return self._swap_to_leveraged(base_position, lev_position, curdatetime)

        # No action needed, stay in same state
        return self

    def _swap_to_leveraged(
        self,
        base_position: Position,
        lev_position: Position,
        curdatetime: datetime,
    ) -> HoldingLeveraged | HoldingNonLeveraged:
        """Sell non-leveraged, buy leveraged using market orders."""
        time.sleep(2)
        base_position, lev_position = get_current_positions()
        initial_base_value = base_position.quantity * base_position.currentPrice

        # Step 1: Sell non-leveraged
        try:
            if base_position.quantity > 0.1:
                place_market_order(
                    MarketOrder(
                        ticker=BASE_TICKER,
                        quantity=base_position.quantity - 0.1,
                        type=MarketOrderType.SELL,
                    )
                )
                send_message("Market sell order placed for non-leveraged")
        except Exception as e:
            send_message(f"Error selling non-leveraged: {str(e)}, staying in HoldingNonLeveraged")
            return HoldingNonLeveraged(signal_data=self.signal_data)

        # Wait and verify sell completed
        time.sleep(5)
        base_position, lev_position = get_current_positions()
        new_base_value = base_position.quantity * base_position.currentPrice

        if new_base_value >= initial_base_value - 5:  # Sell didn't go through
            send_message("Sell non-leveraged order not filled, staying in HoldingNonLeveraged")
            return HoldingNonLeveraged(signal_data=self.signal_data)

        send_message("Sold non-leveraged successfully")

        # Step 2: Buy leveraged
        account_summary = fetch_account_summary()
        if account_summary.cash.availableToTrade < 10:
            send_message("No cash available after sell, staying in HoldingNonLeveraged")
            return HoldingNonLeveraged(signal_data=self.signal_data)

        base_position, lev_position = get_current_positions()
        initial_lev_value = lev_position.quantity * lev_position.currentPrice
        quantity = account_summary.cash.availableToTrade / lev_position.currentPrice

        try:
            place_market_order(
                MarketOrder(
                    ticker=LEV_TICKER,
                    quantity=quantity * 0.9,
                    type=MarketOrderType.BUY,
                )
            )
            send_message("Market buy order placed for leveraged")
        except Exception as e:
            send_message(f"Error buying leveraged: {str(e)}, staying in HoldingNonLeveraged")
            return HoldingNonLeveraged(signal_data=self.signal_data)

        # Wait and verify buy completed
        time.sleep(5)
        base_position, lev_position = get_current_positions()
        new_lev_value = lev_position.quantity * lev_position.currentPrice

        if new_lev_value <= initial_lev_value + 5:  # Buy didn't go through
            send_message("Buy leveraged order not filled, staying in HoldingNonLeveraged")
            return HoldingNonLeveraged(signal_data=self.signal_data)

        send_message("Bought leveraged successfully. Swap complete.")
        return HoldingLeveraged(
            signal_data=SignalData(
                time_last_base_change=curdatetime,
                base_value_at_last_change=base_position.currentPrice,
                lev_value_at_last_change=lev_position.currentPrice,
                position_entry_price=lev_position.currentPrice,
            )
        )


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
            position_entry_price=0.0,
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
