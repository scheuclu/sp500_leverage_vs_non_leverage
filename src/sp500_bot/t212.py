import datetime
import logging
import os
import time
from enum import Enum
from typing import Any

import requests
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from sp500_bot.models import (
    Order,
    Position,
    AccountSummary,
    TradableInstrument,
    Exchange,
    HistoricalOrder,
    PaginatedResponseHistoricalOrder,
)
from sp500_bot.tgbot import send_message
from urllib.parse import urlencode

load_dotenv()

logging.basicConfig(
    level=logging.DEBUG,
    format="{levelname}:{name}:{filename}:{lineno}: {message}",
    style="{",
    force=True,
)

logger = logging.getLogger(__name__)


def _log_request(method: str, url: str, payload: dict[str, Any] | None = None) -> None:
    """Log outgoing API request details."""
    if payload:
        logger.info(f"API REQUEST: {method} {url} | payload={payload}")
    else:
        logger.info(f"API REQUEST: {method} {url}")


def _log_response(response: requests.Response, truncate_body: int = 500) -> None:
    """Log API response details including status and body."""
    body = (
        response.text[:truncate_body] + "..."
        if len(response.text) > truncate_body
        else response.text
    )
    logger.info(f"API RESPONSE: status={response.status_code} | body={body}")


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
        "account_summary": 5.0,
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
        "positions": 5.0,
        "historical_orders": 10.0,
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
    _log_request("DELETE", url)
    response = requests.delete(url, headers=headers)
    _log_response(response)
    response.raise_for_status()
    return response.status_code == 200


def fetch_open_orders() -> list[Order]:
    url = "https://demo.trading212.com/api/v0/equity/orders"
    _rate_limiter.wait("orders_get")
    _log_request("GET", url)
    response = requests.get(url, headers=headers)
    _log_response(response)
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
    _log_request("POST", url, payload)
    response = requests.post(url, json=payload, headers=headers)
    _log_response(response)
    response.raise_for_status()

    data = response.json()
    return Order(**data)


def place_sell_order(ticker: Trading212Ticker, quantity: float, stop_price: float) -> Order:
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
    _log_request("POST", url, payload)
    response = requests.post(url, json=payload, headers=headers)
    _log_response(response)
    response.raise_for_status()

    data = response.json()
    return Order(**data)


def fetch_positions() -> list[Position]:
    url = "https://demo.trading212.com/api/v0/equity/positions"
    _rate_limiter.wait("positions")
    _log_request("GET", url)
    response = requests.get(url, headers=headers)
    _log_response(response)
    response.raise_for_status()

    return [Position(**d) for d in response.json()]


def fetch_single_holding(ticker: Trading212Ticker) -> Position | None:
    url = "https://demo.trading212.com/api/v0/equity/portfolio/ticker"

    payload = {"ticker": ticker.value}
    _rate_limiter.wait("portfolio_ticker")
    _log_request("POST", url, payload)
    response = requests.post(url, json=payload, headers=headers)
    _log_response(response)
    if response.status_code == 404:
        logger.info(f"No holding found for ticker {ticker.value}")
        return None
    response.raise_for_status()
    return Position(**response.json())


def fetch_account_summary() -> AccountSummary:
    url = "https://demo.trading212.com/api/v0/equity/account/summary"
    _rate_limiter.wait("account_summary")
    _log_request("GET", url)
    response = requests.get(url, headers=headers)
    _log_response(response)
    response.raise_for_status()

    return AccountSummary(**response.json())


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
    _log_request("POST", url, payload)
    send_message(f"Placing limit order: {payload}")

    response = requests.post(url, json=payload, headers=headers)
    _log_response(response)
    send_message(f"Limit order response: {response.text}")
    response.raise_for_status()

    data = response.json()
    logger.info(f"Limit order created: id={data.get('id')}")
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
    _log_request("POST", url, payload)
    send_message(f"Placing market order: {payload}")

    response = requests.post(url, json=payload, headers=headers)
    _log_response(response)
    send_message(f"Market order response: {response.text}")
    response.raise_for_status()

    data = response.json()
    logger.info(f"Market order created: id={data.get('id')}")
    return Order(**data)


def fetch_open_order(order_id: int) -> Order:
    url = f"https://demo.trading212.com/api/v0/equity/orders/{order_id}"
    _rate_limiter.wait("order_by_id")
    _log_request("GET", url)
    response = requests.get(url, headers=headers)
    _log_response(response)
    response.raise_for_status()

    data = response.json()
    return Order(**data)


def has_order_been_filled(order_id: int) -> bool:
    url = f"https://demo.trading212.com/api/v0/equity/orders/{order_id}"
    _rate_limiter.wait("order_by_id")
    _log_request("GET", url)
    response = requests.get(url, headers=headers)
    _log_response(response)
    filled = response.status_code == 404
    logger.info(f"Order {order_id} filled: {filled}")
    return filled


def fetch_instruments() -> dict[str, TradableInstrument]:
    url = "https://demo.trading212.com/api/v0/equity/metadata/instruments"
    _rate_limiter.wait("instruments")
    _log_request("GET", url)
    response = requests.get(url, headers=headers)
    _log_response(response, truncate_body=200)  # Large response, truncate more
    response.raise_for_status()
    instruments = {d["ticker"]: TradableInstrument(**d) for d in response.json()}
    logger.info(f"Fetched {len(instruments)} instruments")
    return instruments


def fetch_exchanges() -> list[Exchange]:
    url = "https://demo.trading212.com/api/v0/equity/metadata/exchanges"
    _rate_limiter.wait("exchanges")
    _log_request("GET", url)
    response = requests.get(url, headers=headers)
    _log_response(response)
    response.raise_for_status()
    exchanges = [Exchange(**d) for d in response.json()]
    logger.info(f"Fetched {len(exchanges)} exchanges")
    return exchanges


def fetch_historical_orders(
    ticker: Trading212Ticker, start_date: datetime.date | None, end_date: datetime.date | None
) -> list[HistoricalOrder]:
    """Fetch historical orders for a ticker within a date range.

    Uses cursor-based pagination to retrieve orders from the Trading 212 API.
    The cursor starts at end_date and paginates backwards, filtering results
    to only include orders created on start_date.

    Args:
        ticker: The Trading 212 ticker to fetch orders for.
        start_date: Only return orders created on this date.
        end_date: Start pagination cursor from end of this date.

    Returns:
        List of HistoricalOrder objects matching the ticker and start_date.
    """
    import requests

    url = "https://demo.trading212.com/api/v0/equity/history/orders"

    # start_time = int(1000* datetime.datetime.combine(start_date, datetime.datetime.min.time()).timestamp())
    end_time = int(
        1000 * datetime.datetime.combine(end_date, datetime.datetime.max.time()).timestamp()  # type: ignore[arg-type]
    )

    results: list[HistoricalOrder] = []

    query = {
        "cursor": end_time,  # ms
        "ticker": ticker.value,
        "limit": "50",  # maximum 50
    }
    nextPagePath = f"{url}?{urlencode(query=query)}"

    page_num = 0
    while nextPagePath:
        _log_request("GET", nextPagePath)
        response = requests.get(nextPagePath, headers=headers)
        _log_response(response)
        response.raise_for_status()
        _rate_limiter.wait("historical_orders")
        page_num += 1

        paginated = PaginatedResponseHistoricalOrder(**response.json())
        logger.info(f"Historical orders page {page_num}: {len(paginated.items)} items")
        if len(paginated.items) == 0:
            nextPagePath = None
        else:
            nextPagePath = f"https://demo.trading212.com{paginated.nextPagePath}"
            orders = [p for p in paginated.items if p.order.createdAt.date() == start_date]
            results += orders
            if len(orders) < len(paginated.items):
                nextPagePath = None  # Some orders where at a different date

    logger.info(f"Fetched {len(results)} historical orders for {ticker.value}")
    return results


if __name__ == "__main__":
    positions = fetch_positions()

    historical_orders = fetch_historical_orders(
        ticker=Trading212Ticker.SP500_EUR,
        start_date=datetime.date(2026, 1, 8),
        end_date=datetime.date(2026, 1, 8),
    )
    for order in historical_orders:
        print(order)
