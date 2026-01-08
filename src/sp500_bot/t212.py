import logging
import time
from enum import Enum

import requests
from pydantic import BaseModel, Field

from sp500_bot.models import Order, Position, Cash, TradableInstrument, Exchange
from sp500_bot.tgbot import send_message
from dotenv import load_dotenv

load_dotenv()
import os

logging.basicConfig(
    level=logging.DEBUG,
    format="{levelname}:{name}:{filename}:{lineno}: {message}",
    style="{",
    force=True,
)

TRADING212_KEY = os.environ["TRADING212_KEY"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]

headers = {
    "Authorization": TRADING212_KEY,
    "Content-Type": "application/json",
}


class RateLimiter:
    """Rate limiter that tracks last call time per endpoint and sleeps if needed."""

    # Rate limits from api.json (in seconds)
    LIMITS: dict[str, float] = {
        "account_cash": 2.0,
        "account_info": 30.0,
        "exchanges": 30.0,
        "instruments": 50.0,
        "orders_get": 5.0,
        "orders_limit": 2.0,
        "orders_market": 1.2,  # 50 per minute = 1.2s between calls
        "orders_stop": 2.0,
        "orders_cancel": 1.2,  # 50 per minute = 1.2s between calls
        "order_by_id": 1.0,
        "portfolio": 5.0,
        "portfolio_ticker": 1.0,
    }

    def __init__(self):
        self._last_call: dict[str, float] = {}

    def wait(self, endpoint: str) -> None:
        """Wait if necessary before making a call to the given endpoint."""
        if endpoint not in self.LIMITS:
            logging.warning(f"Unknown endpoint for rate limiting: {endpoint}")
            return

        limit = self.LIMITS[endpoint]
        last = self._last_call.get(endpoint, 0.0)
        elapsed = time.time() - last
        wait_time = limit - elapsed

        if wait_time > 0:
            logging.debug(f"Rate limit: waiting {wait_time:.2f}s for {endpoint}")
            time.sleep(wait_time)

        self._last_call[endpoint] = time.time()


# Global rate limiter instance
_rate_limiter = RateLimiter()


class Trading212Ticker(Enum):
    """Ticker string used for all trading212 endpoints"""

    SP500_ACC = "VUAGl_EQ"
    SP500_5L = "5LUSl_EQ"
    SP500_EUR = "VUAAm_EQ"
    SP500_EUR_ISHARES = "SXR8d_EQ"
    SP500_EUR_L = "US5Ld_EQ"


def cancel_order_by_id(order_id: int) -> bool:
    url = "https://demo.trading212.com/api/v0/equity/orders/" + str(order_id)
    _rate_limiter.wait("orders_cancel")
    logging.debug(f"Calling cancel_order_by_id({order_id})")
    response = requests.delete(url, headers=headers)
    response.raise_for_status()
    return response.status_code == 200


def fetch_open_orders() -> list[Order]:
    url = "https://demo.trading212.com/api/v0/equity/orders"
    _rate_limiter.wait("orders_get")
    logging.debug("Calling fetch_open_orders()")
    response = requests.get(url, headers=headers)
    response.raise_for_status()

    data = response.json()
    return [Order(**d) for d in data]


def cancel_open_orders():
    open_orders = fetch_open_orders()
    for order in open_orders:
        if order.id:
            assert cancel_order_by_id(order.id)


def place_buy_order(ticker: Trading212Ticker, quantity: float) -> Order:
    """Buying asset with specific ticker on Trading212"""
    url = "https://demo.trading212.com/api/v0/equity/orders/market"

    payload = {
        "quantity": quantity,
        "ticker": ticker.value,  # "AAPL_US_EQ"
    }
    _rate_limiter.wait("orders_market")
    logging.debug(f"Calling place_buy_order({ticker}, {quantity})")
    response = requests.post(url, json=payload, headers=headers)
    response.raise_for_status()

    data = response.json()
    return Order(**data)


def place_sell_order(
    ticker: Trading212Ticker, quantity: float, stop_price: float
) -> Order:
    """Selling an asset with specific ticker on Trading212.

    The `quantity` needs to be negative.
    """
    url = "https://demo.trading212.com/api/v0/equity/orders/stop"

    payload = {
        "quantity": quantity,  # 0.01
        "stopPrice": stop_price,  # 2960
        "ticker": ticker.value,
        "timeValidity": "DAY",
    }
    _rate_limiter.wait("orders_stop")
    logging.debug(f"Calling place_sell_order({ticker}, {quantity}, {stop_price})")
    response = requests.post(url, json=payload, headers=headers)
    response.raise_for_status()

    data = response.json()
    return Order(**data)


def fetch_positions() -> list[Position]:
    url = "https://demo.trading212.com/api/v0/equity/portfolio"
    _rate_limiter.wait("portfolio")
    logging.debug("Calling fetch_positions()")
    response = requests.get(url, headers=headers)
    response.raise_for_status()

    return [Position(**d) for d in response.json()]


def fetch_single_holding(ticker: Trading212Ticker) -> Position | None:
    url = "https://demo.trading212.com/api/v0/equity/portfolio/ticker"

    payload = {"ticker": ticker.value}
    _rate_limiter.wait("portfolio_ticker")
    logging.debug(f"Calling fetch_single_holding({ticker})")
    response = requests.post(url, json=payload, headers=headers)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return Position(**response.json())


def fetch_account_cash() -> Cash:
    url = "https://demo.trading212.com/api/v0/equity/account/cash"
    _rate_limiter.wait("account_cash")
    logging.debug("Calling fetch_account_cash()")
    response = requests.get(url, headers=headers)
    response.raise_for_status()

    return Cash(**response.json())


class LimitOrderType(Enum):
    BUY = 0
    SELL = 1


class MarketOrderType(Enum):
    BUY = 0
    SELL = 1


class LimitOrder(BaseModel):
    ticker: Trading212Ticker
    quantity: float = Field(gt=0.0)
    limit_price: float = Field(gt=0.0)
    type: LimitOrderType


class MarketOrder(BaseModel):
    ticker: Trading212Ticker
    quantity: float = Field(gt=0.0)
    type: MarketOrderType


def place_limit_order(order: LimitOrder) -> Order:
    """Selling means negative quantity"""
    url = "https://demo.trading212.com/api/v0/equity/orders/limit"

    payload = {
        "quantity": round(order.quantity, 2)
        if order.type == LimitOrderType.BUY
        else round(-order.quantity, 2),  # 0.01
        "limitPrice": round(order.limit_price, 3),  # 2960
        "ticker": order.ticker.value,
        "timeValidity": "DAY",
    }

    _rate_limiter.wait("orders_limit")
    logging.debug(f"Calling place_limit_order({order})")
    logging.info(payload)

    send_message(f"Placing limit order: {payload}")

    response = requests.post(url, json=payload, headers=headers)
    send_message(f".... {response.text}")
    response.raise_for_status()

    data = response.json()
    logging.info(data)
    return Order(**data)


def place_market_order(order: MarketOrder) -> Order:
    """Selling means negative quantity"""
    url = "https://demo.trading212.com/api/v0/equity/orders/market"

    payload = {
        "extendedHours": False,
        "quantity": round(order.quantity, 2)
        if order.type == MarketOrderType.BUY
        else round(-order.quantity, 2),  # 0.01
        "ticker": order.ticker.value,
    }

    _rate_limiter.wait("orders_market")
    logging.debug(f"Calling place_market_order({order})")
    logging.info(payload)

    message = f"Placing market order: {payload}"
    logging.info(f"Sending message: {message}")
    send_message(message)

    response = requests.post(url, json=payload, headers=headers)
    send_message(f".... {response.text}")
    response.raise_for_status()

    data = response.json()
    return Order(**data)


def fetch_open_order(order_id: int) -> Order:
    url = f"https://demo.trading212.com/api/v0/equity/orders/{order_id}"
    _rate_limiter.wait("order_by_id")
    logging.debug(f"Calling fetch_open_order({order_id})")
    response = requests.get(url, headers=headers)
    response.raise_for_status()

    data = response.json()
    return Order(**data)


def has_order_been_filled(order_id: int) -> bool:
    url = f"https://demo.trading212.com/api/v0/equity/orders/{order_id}"
    _rate_limiter.wait("order_by_id")
    logging.debug(f"Calling has_order_been_filled({order_id})")
    response = requests.get(url, headers=headers)
    return response.status_code == 404


def fetch_instruments() -> dict[str, TradableInstrument]:
    url = "https://demo.trading212.com/api/v0/equity/metadata/instruments"
    _rate_limiter.wait("instruments")
    logging.debug("Calling fetch_instruments()")
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    instruments = {d["ticker"]: TradableInstrument(**d) for d in response.json()}
    return instruments


def fetch_exchanges() -> list[Exchange]:
    url = "https://demo.trading212.com/api/v0/equity/metadata/exchanges"
    _rate_limiter.wait("exchanges")
    logging.debug("Calling fetch_exchanges()")
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    exchanges = [Exchange(**d) for d in response.json()]
    return exchanges


if __name__ == "__main__":
    order: Order = place_market_order(
        MarketOrder(
            ticker=Trading212Ticker.SP500_ACC, quantity=20.0, type=MarketOrderType.BUY
        )
    )
