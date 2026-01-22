from __future__ import annotations

import os
import logging
from logging.handlers import RotatingFileHandler
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


def setup_logging():
    """Configure logging with both console and rotating file handlers."""
    log_format = "{levelname}:{name}:{filename}:{lineno}: {message}"
    formatter = logging.Formatter(log_format, style="{")

    # Get root logger to capture all logs (including from t212.py)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # Clear any existing handlers
    root_logger.handlers.clear()

    # Console handler (INFO level)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # Rotating file handler (DEBUG level for more detail)
    # 5 MB per file, keep 5 backup files (25 MB total max)
    file_handler = RotatingFileHandler(
        "trading_bot.log",
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)


setup_logging()
logger = logging.getLogger(__name__)

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
        logger.info("=== INITIALIZING STATE ===")
        logger.info("Cancelling any open orders...")
        cancel_open_orders()
        logger.info("Waiting 10s after order cancellation...")
        time.sleep(10)
        logger.info("Fetching current positions...")
        base_position, lev_position = get_current_positions()
        logger.info("Fetching account summary...")
        account_summary: AccountSummary = fetch_account_summary()

        # Determine current holdings
        base_value = base_position.quantity * base_position.currentPrice
        lev_value = lev_position.quantity * lev_position.currentPrice
        cash_available = account_summary.cash.availableToTrade

        logger.info(
            f"Init holdings: base_qty={base_position.quantity:.4f} @ {base_position.currentPrice:.2f} = {base_value:.2f}"
        )
        logger.info(
            f"Init holdings: lev_qty={lev_position.quantity:.4f} @ {lev_position.currentPrice:.2f} = {lev_value:.2f}"
        )
        logger.info(f"Init holdings: cash_available={cash_available:.2f}")

        # If we have significant non-leveraged holdings, stay in that position
        if base_value > lev_value and base_value > cash_available:
            logger.info(
                f"Decision: Already holding non-leveraged (base_value={base_value:.2f} > lev={lev_value:.2f}, cash={cash_available:.2f})"
            )
            send_message(f"Initialized: Already holding non-leveraged (value={base_value:.2f})")
            logger.info("STATE TRANSITION: Initializing -> HoldingNonLeveraged")
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
            logger.info(
                f"Decision: Already holding leveraged (lev_value={lev_value:.2f} > base={base_value:.2f}, cash={cash_available:.2f})"
            )
            send_message(f"Initialized: Already holding leveraged (value={lev_value:.2f})")
            logger.info("STATE TRANSITION: Initializing -> HoldingLeveraged")
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
            logger.info(f"Decision: Have cash ({cash_available:.2f}), buying leveraged")
            send_message(f"Initialized: Buying leveraged with cash={cash_available:.2f}")
            quantity = cash_available / lev_position.currentPrice
            logger.info(f"Calculated buy quantity: {quantity:.4f} (will use {quantity * 0.9:.4f})")
            try:
                logger.info(
                    f"Placing market BUY order for {LEV_TICKER.value}, qty={quantity * 0.9:.4f}"
                )
                place_market_order(
                    MarketOrder(
                        ticker=LEV_TICKER,
                        quantity=quantity * 0.9,
                        type=MarketOrderType.BUY,
                    )
                )
                logger.info("Market buy order placed successfully")
                send_message("Market buy order placed for leveraged")
            except Exception as e:
                logger.error(f"Init buy order FAILED: {e}", exc_info=True)
                send_message(f"Init buy error: {str(e)}, staying in Initializing")
                return Initializing(signal_data=self.signal_data)

            # Wait and verify the order was filled by checking positions
            logger.info("Waiting 5s for order to fill...")
            time.sleep(5)
            logger.info("Verifying order fill by checking positions...")
            base_position, lev_position = get_current_positions()
            new_lev_value = lev_position.quantity * lev_position.currentPrice
            logger.info(
                f"Position verification: old_lev_value={lev_value:.2f}, new_lev_value={new_lev_value:.2f}"
            )

            if new_lev_value <= lev_value + 5:  # Order didn't fill (no significant increase)
                logger.warning(
                    f"Order NOT filled: new_lev_value ({new_lev_value:.2f}) <= old_lev_value + 5 ({lev_value + 5:.2f})"
                )
                send_message("Init buy order not filled, staying in Initializing")
                return Initializing(signal_data=self.signal_data)

            logger.info("Order fill verified")
            send_message("Initialized: Holding leveraged (bought successfully)")
            logger.info("STATE TRANSITION: Initializing -> HoldingLeveraged")
            return HoldingLeveraged(
                signal_data=SignalData(
                    time_last_base_change=curdatetime,
                    base_value_at_last_change=base_position.currentPrice,
                    lev_value_at_last_change=lev_position.currentPrice,
                    position_entry_price=lev_position.currentPrice,
                )
            )

        # No cash and no significant holdings - stay in Initializing
        logger.warning(
            f"No significant holdings or cash to trade: base={base_value:.2f}, lev={lev_value:.2f}, cash={cash_available:.2f}"
        )
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
        logger.info("--- HoldingLeveraged.process() ---")
        logger.info(
            f"Current prices: base={base_position.currentPrice}, lev={lev_position.currentPrice}"
        )
        logger.info(
            f"Reference prices: base_ref={self.signal_data.base_value_at_last_change}, lev_ref={self.signal_data.lev_value_at_last_change}"
        )

        # If base price changed, update reference and stay
        if base_position.currentPrice != self.signal_data.base_value_at_last_change:
            logger.info(
                f"Base price changed: {self.signal_data.base_value_at_last_change} -> {base_position.currentPrice}"
            )
            logger.info("Resetting divergence reference, staying in HoldingLeveraged")
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
        logger.info(
            f"Divergence calc: lev_current={lev_position.currentPrice}, lev_ref={self.signal_data.lev_value_at_last_change}"
        )
        logger.info(
            f"Lev divergence: {lev_diff_rel:.6f} (threshold: {LEV_DIFF_INVEST}) | Time: {time_since_base_change} (threshold: {TIME_DIFF_INVEST})"
        )

        # If leveraged diverged UP significantly for long enough → swap to non-leveraged
        if lev_diff_rel > LEV_DIFF_INVEST and time_since_base_change > TIME_DIFF_INVEST:
            logger.info(
                f"SWAP TRIGGER: Leveraged diverged UP ({lev_diff_rel:.4f} > {LEV_DIFF_INVEST}) for {time_since_base_change}"
            )
            send_message(
                f"Leveraged overperforming (+{lev_diff_rel:.4f}). Swapping to non-leveraged."
            )
            return self._swap_to_non_leveraged(base_position, lev_position, curdatetime)

        # No action needed, stay in same state
        logger.info("No swap conditions met, staying in HoldingLeveraged")
        return self

    def _swap_to_non_leveraged(
        self,
        base_position: Position,
        lev_position: Position,
        curdatetime: datetime,
    ) -> HoldingNonLeveraged | HoldingLeveraged:
        """Sell leveraged, buy non-leveraged using market orders."""
        logger.info("=== SWAP: Leveraged -> Non-Leveraged ===")
        logger.info("Waiting 2s before swap...")
        time.sleep(2)
        logger.info("Fetching current positions before swap...")
        base_position, lev_position = get_current_positions()
        initial_lev_value = lev_position.quantity * lev_position.currentPrice
        logger.info(
            f"Initial lev position: qty={lev_position.quantity:.4f}, value={initial_lev_value:.2f}"
        )

        # Step 1: Sell leveraged
        try:
            if lev_position.quantity > 0.01:
                sell_qty = lev_position.quantity - 0.01
                logger.info(f"STEP 1: Placing market SELL order for leveraged: qty={sell_qty:.4f}")
                place_market_order(
                    MarketOrder(
                        ticker=LEV_TICKER,
                        quantity=sell_qty,
                        type=MarketOrderType.SELL,
                    )
                )
                logger.info("Market sell order placed successfully")
                send_message("Market sell order placed for leveraged")
            else:
                logger.warning(
                    f"Insufficient leveraged quantity to sell: {lev_position.quantity:.4f}"
                )
        except Exception as e:
            logger.error(f"SELL leveraged FAILED: {e}", exc_info=True)
            send_message(f"Error selling leveraged: {str(e)}, staying in HoldingLeveraged")
            return HoldingLeveraged(signal_data=self.signal_data)

        # Wait and verify sell completed
        logger.info("Waiting 5s for sell order to fill...")
        time.sleep(5)
        logger.info("Verifying sell order fill...")
        base_position, lev_position = get_current_positions()
        new_lev_value = lev_position.quantity * lev_position.currentPrice
        logger.info(
            f"Sell verification: initial_value={initial_lev_value:.2f}, new_value={new_lev_value:.2f}"
        )

        if new_lev_value >= initial_lev_value - 5:  # Sell didn't go through
            logger.warning(
                f"Sell NOT filled: new_value ({new_lev_value:.2f}) >= initial_value - 5 ({initial_lev_value - 5:.2f})"
            )
            send_message("Sell leveraged order not filled, staying in HoldingLeveraged")
            return HoldingLeveraged(signal_data=self.signal_data)

        logger.info("Sell leveraged verified")
        send_message("Sold leveraged successfully")

        # Step 2: Buy non-leveraged
        logger.info("STEP 2: Fetching account summary for buy...")
        account_summary = fetch_account_summary()
        cash_available = account_summary.cash.availableToTrade
        logger.info(f"Cash available for buy: {cash_available:.2f}")

        if cash_available < 10:
            logger.warning(f"Insufficient cash after sell: {cash_available:.2f}")
            send_message("No cash available after sell, staying in HoldingLeveraged")
            return HoldingLeveraged(signal_data=self.signal_data)

        base_position, lev_position = get_current_positions()
        initial_base_value = base_position.quantity * base_position.currentPrice
        quantity = cash_available / base_position.currentPrice
        logger.info(f"Calculated buy quantity: {quantity:.4f} (will use {quantity * 0.9:.4f})")

        try:
            logger.info(f"Placing market BUY order for non-leveraged: qty={quantity * 0.9:.4f}")
            place_market_order(
                MarketOrder(
                    ticker=BASE_TICKER,
                    quantity=quantity * 0.9,
                    type=MarketOrderType.BUY,
                )
            )
            logger.info("Market buy order placed successfully")
            send_message("Market buy order placed for non-leveraged")
        except Exception as e:
            logger.error(f"BUY non-leveraged FAILED: {e}", exc_info=True)
            send_message(f"Error buying non-leveraged: {str(e)}, staying in HoldingLeveraged")
            return HoldingLeveraged(signal_data=self.signal_data)

        # Wait and verify buy completed
        logger.info("Waiting 5s for buy order to fill...")
        time.sleep(5)
        logger.info("Verifying buy order fill...")
        base_position, lev_position = get_current_positions()
        new_base_value = base_position.quantity * base_position.currentPrice
        logger.info(
            f"Buy verification: initial_value={initial_base_value:.2f}, new_value={new_base_value:.2f}"
        )

        if new_base_value <= initial_base_value + 5:  # Buy didn't go through
            logger.warning(
                f"Buy NOT filled: new_value ({new_base_value:.2f}) <= initial_value + 5 ({initial_base_value + 5:.2f})"
            )
            send_message("Buy non-leveraged order not filled, staying in HoldingLeveraged")
            return HoldingLeveraged(signal_data=self.signal_data)

        logger.info("Buy non-leveraged verified")
        send_message("Bought non-leveraged successfully. Swap complete.")
        logger.info("STATE TRANSITION: HoldingLeveraged -> HoldingNonLeveraged")
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
        logger.info("--- HoldingNonLeveraged.process() ---")
        entry_price = self.signal_data.position_entry_price
        stop_loss_price = entry_price * (1 - STOP_LOSS_THRESHOLD)

        logger.info(
            f"Current prices: base={base_position.currentPrice}, lev={lev_position.currentPrice}"
        )
        logger.info(f"Entry price: {entry_price}, Stop-loss price: {stop_loss_price:.2f}")
        logger.info(
            f"Reference prices: base_ref={self.signal_data.base_value_at_last_change}, lev_ref={self.signal_data.lev_value_at_last_change}"
        )

        # Check for profit: base went UP from entry
        if base_position.currentPrice > entry_price:
            logger.info(
                f"PROFIT TRIGGER: base_current ({base_position.currentPrice}) > entry_price ({entry_price})"
            )
            send_message(
                f"Base price increased ({base_position.currentPrice:.2f} > {entry_price:.2f}). Taking profit, swapping to leveraged."
            )
            return self._swap_to_leveraged(base_position, lev_position, curdatetime)

        # Check for stop-loss
        if base_position.currentPrice < stop_loss_price:
            logger.info(
                f"STOP-LOSS TRIGGER: base_current ({base_position.currentPrice}) < stop_loss ({stop_loss_price:.2f})"
            )
            send_message(
                f"Stop-loss triggered! Price {base_position.currentPrice:.2f} < {stop_loss_price:.2f}. Swapping to leveraged."
            )
            return self._swap_to_leveraged(base_position, lev_position, curdatetime)

        # If base price changed (but not up from entry), update divergence reference
        if base_position.currentPrice != self.signal_data.base_value_at_last_change:
            logger.info(
                f"Base price changed: {self.signal_data.base_value_at_last_change} -> {base_position.currentPrice}"
            )
            logger.info("Resetting divergence reference, staying in HoldingNonLeveraged")
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
        logger.info(
            f"Divergence calc: lev_current={lev_position.currentPrice}, lev_ref={self.signal_data.lev_value_at_last_change}"
        )
        logger.info(
            f"Lev divergence: {lev_diff_rel:.6f} (threshold: -{LEV_DIFF_INVEST}) | Time: {time_since_base_change} (threshold: {TIME_DIFF_INVEST})"
        )

        # Check for negative divergence: leveraged underperforming → swap back to capture recovery
        if lev_diff_rel < -LEV_DIFF_INVEST and time_since_base_change > TIME_DIFF_INVEST:
            logger.info(
                f"NEGATIVE DIVERGENCE TRIGGER: Leveraged diverged DOWN ({lev_diff_rel:.4f} < -{LEV_DIFF_INVEST}) for {time_since_base_change}"
            )
            send_message(
                f"Leveraged underperforming ({lev_diff_rel:.4f}). Swapping to leveraged to capture recovery."
            )
            return self._swap_to_leveraged(base_position, lev_position, curdatetime)

        # No action needed, stay in same state
        logger.info("No swap conditions met, staying in HoldingNonLeveraged")
        return self

    def _swap_to_leveraged(
        self,
        base_position: Position,
        lev_position: Position,
        curdatetime: datetime,
    ) -> HoldingLeveraged | HoldingNonLeveraged:
        """Sell non-leveraged, buy leveraged using market orders."""
        logger.info("=== SWAP: Non-Leveraged -> Leveraged ===")
        logger.info("Waiting 2s before swap...")
        time.sleep(2)
        logger.info("Fetching current positions before swap...")
        base_position, lev_position = get_current_positions()
        initial_base_value = base_position.quantity * base_position.currentPrice
        logger.info(
            f"Initial base position: qty={base_position.quantity:.4f}, value={initial_base_value:.2f}"
        )

        # Step 1: Sell non-leveraged
        try:
            if base_position.quantity > 0.1:
                sell_qty = base_position.quantity - 0.1
                logger.info(
                    f"STEP 1: Placing market SELL order for non-leveraged: qty={sell_qty:.4f}"
                )
                place_market_order(
                    MarketOrder(
                        ticker=BASE_TICKER,
                        quantity=sell_qty,
                        type=MarketOrderType.SELL,
                    )
                )
                logger.info("Market sell order placed successfully")
                send_message("Market sell order placed for non-leveraged")
            else:
                logger.warning(
                    f"Insufficient non-leveraged quantity to sell: {base_position.quantity:.4f}"
                )
        except Exception as e:
            logger.error(f"SELL non-leveraged FAILED: {e}", exc_info=True)
            send_message(f"Error selling non-leveraged: {str(e)}, staying in HoldingNonLeveraged")
            return HoldingNonLeveraged(signal_data=self.signal_data)

        # Wait and verify sell completed
        logger.info("Waiting 5s for sell order to fill...")
        time.sleep(5)
        logger.info("Verifying sell order fill...")
        base_position, lev_position = get_current_positions()
        new_base_value = base_position.quantity * base_position.currentPrice
        logger.info(
            f"Sell verification: initial_value={initial_base_value:.2f}, new_value={new_base_value:.2f}"
        )

        if new_base_value >= initial_base_value - 5:  # Sell didn't go through
            logger.warning(
                f"Sell NOT filled: new_value ({new_base_value:.2f}) >= initial_value - 5 ({initial_base_value - 5:.2f})"
            )
            send_message("Sell non-leveraged order not filled, staying in HoldingNonLeveraged")
            return HoldingNonLeveraged(signal_data=self.signal_data)

        logger.info("Sell non-leveraged verified")
        send_message("Sold non-leveraged successfully")

        # Step 2: Buy leveraged
        logger.info("STEP 2: Fetching account summary for buy...")
        account_summary = fetch_account_summary()
        cash_available = account_summary.cash.availableToTrade
        logger.info(f"Cash available for buy: {cash_available:.2f}")

        if cash_available < 10:
            logger.warning(f"Insufficient cash after sell: {cash_available:.2f}")
            send_message("No cash available after sell, staying in HoldingNonLeveraged")
            return HoldingNonLeveraged(signal_data=self.signal_data)

        base_position, lev_position = get_current_positions()
        initial_lev_value = lev_position.quantity * lev_position.currentPrice
        quantity = cash_available / lev_position.currentPrice
        logger.info(f"Calculated buy quantity: {quantity:.4f} (will use {quantity * 0.9:.4f})")

        try:
            logger.info(f"Placing market BUY order for leveraged: qty={quantity * 0.9:.4f}")
            place_market_order(
                MarketOrder(
                    ticker=LEV_TICKER,
                    quantity=quantity * 0.9,
                    type=MarketOrderType.BUY,
                )
            )
            logger.info("Market buy order placed successfully")
            send_message("Market buy order placed for leveraged")
        except Exception as e:
            logger.error(f"BUY leveraged FAILED: {e}", exc_info=True)
            send_message(f"Error buying leveraged: {str(e)}, staying in HoldingNonLeveraged")
            return HoldingNonLeveraged(signal_data=self.signal_data)

        # Wait and verify buy completed
        logger.info("Waiting 5s for buy order to fill...")
        time.sleep(5)
        logger.info("Verifying buy order fill...")
        base_position, lev_position = get_current_positions()
        new_lev_value = lev_position.quantity * lev_position.currentPrice
        logger.info(
            f"Buy verification: initial_value={initial_lev_value:.2f}, new_value={new_lev_value:.2f}"
        )

        if new_lev_value <= initial_lev_value + 5:  # Buy didn't go through
            logger.warning(
                f"Buy NOT filled: new_value ({new_lev_value:.2f}) <= initial_value + 5 ({initial_lev_value + 5:.2f})"
            )
            send_message("Buy leveraged order not filled, staying in HoldingNonLeveraged")
            return HoldingNonLeveraged(signal_data=self.signal_data)

        logger.info("Buy leveraged verified")
        send_message("Bought leveraged successfully. Swap complete.")
        logger.info("STATE TRANSITION: HoldingNonLeveraged -> HoldingLeveraged")
        return HoldingLeveraged(
            signal_data=SignalData(
                time_last_base_change=curdatetime,
                base_value_at_last_change=base_position.currentPrice,
                lev_value_at_last_change=lev_position.currentPrice,
                position_entry_price=lev_position.currentPrice,
            )
        )


def get_current_positions() -> tuple[Position, Position]:
    """Fetch and return current positions for base and leveraged tickers."""
    logger.debug("Fetching current positions...")
    ticker_values: list[str] = [i.value for i in Trading212Ticker.__members__.values()]
    positions: dict[Trading212Ticker, Position] = {
        p.instrument.ticker: p
        for p in fetch_positions()
        if p.instrument and p.instrument.ticker and p.instrument.ticker in ticker_values
    }
    base_position: Position = positions[BASE_TICKER.value]
    lev_position: Position = positions[LEV_TICKER.value]
    logger.debug(
        f"Positions: base_qty={base_position.quantity}, base_price={base_position.currentPrice} | lev_qty={lev_position.quantity}, lev_price={lev_position.currentPrice}"
    )
    return base_position, lev_position


def main():
    logger.info("========================================")
    logger.info("Starting SP500 Trading Bot")
    logger.info("========================================")
    logger.info(
        f"Configuration: LEV_DIFF_INVEST={LEV_DIFF_INVEST}, TIME_DIFF_INVEST={TIME_DIFF_INVEST}"
    )
    logger.info(f"Configuration: STOP_LOSS_THRESHOLD={STOP_LOSS_THRESHOLD}")
    logger.info(f"Configuration: BASE_TICKER={BASE_TICKER.value}, LEV_TICKER={LEV_TICKER.value}")

    logger.info("Fetching instruments...")
    instruments = fetch_instruments()
    logger.info("Fetching exchanges...")
    exchanges = fetch_exchanges()

    INTERVAL = 20  # seconds
    logger.info(f"Configuration: INTERVAL={INTERVAL}s")
    next_run = time.time()

    logger.info("Initializing trader state...")
    trader_state: TraderState = Initializing(
        signal_data=SignalData(
            time_last_base_change=datetime.now(),
            base_value_at_last_change=0.0,
            lev_value_at_last_change=0.0,
            position_entry_price=0.0,
        )
    )
    logger.info("Starting main trading loop...")

    while True:
        logger.info("=" * 50)
        logger.info(f"LOOP ITERATION: Current state = {trader_state.__class__.__name__}")
        logger.info(f"Time: {datetime.now().isoformat()}")

        base_position, lev_position = get_current_positions()

        all_open: bool = are_positions_tradeable(
            exchanges, instruments, [base_position, lev_position]
        )
        if not all_open:
            logger.info("Markets not all open - sleeping 300s")
            time.sleep(300)
            continue

        logger.info("Markets open - processing...")
        write_positions([base_position, lev_position])
        curdatetime = datetime.now()

        old_state = trader_state.__class__.__name__
        trader_state = trader_state.process(base_position, lev_position, curdatetime)
        new_state = trader_state.__class__.__name__

        if old_state != new_state:
            logger.info(f"STATE CHANGED: {old_state} -> {new_state}")

        # Write current state to Supabase
        logger.debug("Writing state to Supabase...")
        write_state(
            state_name=trader_state.__class__.__name__,
            time_last_base_change=trader_state.signal_data.time_last_base_change,
            base_value_at_last_change=trader_state.signal_data.base_value_at_last_change,
            lev_value_at_last_change=trader_state.signal_data.lev_value_at_last_change,
        )

        # Schedule the next run based on absolute time
        next_run += INTERVAL
        sleep_time = next_run - time.time()
        logger.info(f"Loop complete. Sleeping for {round(sleep_time, 4)}s until next iteration.")
        if sleep_time > 0:
            time.sleep(sleep_time)
        else:  # If we're running behind schedule, skip missed intervals
            logger.warning(f"Running behind schedule by {-sleep_time:.2f}s, resetting timer")
            next_run = time.time()


if __name__ == "__main__":
    main()
