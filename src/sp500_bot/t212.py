import logging
import sys

import requests
from sp500_bot.models import Order, Position, Cash, TradableInstrument, Exchange
from dotenv import load_dotenv

load_dotenv()
import os
from pydantic import BaseModel, Field
from sp500_bot.tgbot import send_message

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


from enum import Enum


class Trading212Ticker(Enum):
    """Ticker string used for all trading212 endpoints"""

    SP500_ACC = "VUAGl_EQ"
    SP500_5L = "5LUSl_EQ"
    SP500_EUR = "VUAAm_EQ"
    SP500_EUR_ISHARES = "SXR8d_EQ"
    SP500_EUR_L = "US5Ld_EQ"


def cancel_order_by_id(id: int) -> bool:
    url = "https://demo.trading212.com/api/v0/equity/orders/" + str(id)
    logging.debug(f"Calling cancel_order_by_id({id})")
    response = requests.delete(url, headers=headers)
    response.raise_for_status()
    return response.status_code == 200


def fetch_open_orders() -> list[Order]:
    url = "https://demo.trading212.com/api/v0/equity/orders"
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
    logging.debug(f"Calling place_sell_order({ticker}, {quantity}, {stop_price})")
    response = requests.post(url, json=payload, headers=headers)
    response.raise_for_status()

    data = response.json()
    return Order(**data)


def fetch_positions() -> list[Position]:
    logging.debug("Calling fetch_positions()")
    url = "https://demo.trading212.com/api/v0/equity/portfolio"
    sys.stdout.flush()

    response = requests.get(url, headers=headers)
    response.raise_for_status()

    return [Position(**d) for d in response.json()]


def fetch_single_holding(ticker: Trading212Ticker) -> Position | None:
    url = "https://demo.trading212.com/api/v0/equity/portfolio/ticker"

    payload = {"ticker": ticker.value}
    logging.debug(f"Calling fetch_single_holding({ticker})")
    response = requests.post(url, json=payload, headers=headers)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return Position(**response.json())


def fetch_account_cash() -> Cash:
    url = "https://demo.trading212.com/api/v0/equity/account/cash"
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


def fetch_open_order(id: int) -> Order:
    url = f"https://demo.trading212.com/api/v0/equity/orders/{id}"
    logging.debug(f"Calling fetch_open_order({id})")
    response = requests.get(url, headers=headers)
    response.raise_for_status()

    data = response.json()
    return Order(**data)


def has_order_been_filled(id: int):
    url = f"https://demo.trading212.com/api/v0/equity/orders/{id}"
    logging.debug(f"Calling has_order_been_filled({id})")
    response = requests.get(url, headers=headers)
    return response.status_code == 404


def fetch_instruments() -> list[TradableInstrument]:
    url = "https://demo.trading212.com/api/v0/equity/metadata/instruments"
    logging.debug("Calling fetch_instruments()")
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    instruments = {d["ticker"]: TradableInstrument(**d) for d in response.json()}
    return instruments


def fetch_exchanges() -> list[Exchange]:
    url = "https://demo.trading212.com/api/v0/equity/metadata/exchanges"
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

    # result = fetch_single_holding(Trading212Ticker.AAPL)
    # print(result)

    # # Buy as much as possible
    # free_cash: Cash = fetch_account_cash()
    # sp500_position: Position= fetch_single_holding(Trading212Ticker.SP500_ACC)
    #
    # num_buy=round(free_cash.free/sp500_position.currentPrice*0.5,1)
    #
    # # open_orders = fetch_open_orders()
    # order: Order|ToolError = place_limit_order(
    #     LimitOrder(
    #     ticker=Trading212Ticker.SP500_ACC,
    #     quantity=num_buy,
    #     limit_price=round(sp500_position.currentPrice*1.0005,3),
    #     type=LimitOrderType.BUY
    # ))
    # print(order)
    # assert isinstance(order, Order), f"{order}"
    # ID= order.id
    # order_filled=has_order_been_filled(ID)
    # while not order_filled:
    #     print("Order is still open")
    #     order_filled = has_order_been_filled(ID)
    #     time.sleep(1.1) # time limit
    # print("Order has been filled")
    # sp500_position: Position= fetch_single_holding(Trading212Ticker.SP500_ACC)
    # num_sell = round(sp500_position.quantity-0.1, 3)
    #
    # order: Order|ToolError = place_limit_order(
    #     LimitOrder(
    #     ticker=Trading212Ticker.SP500_ACC,
    #     quantity=num_sell,
    #     limit_price=round(sp500_position.currentPrice*0.9998,3),
    #     type=LimitOrderType.SELL
    # ))
    # print(order)
    # assert isinstance(order, Order), f"{order}"
    # ID= order.id
    # order_filled=has_order_been_filled(ID)
    # while not order_filled:
    #     print("Order is still open")
    #     order_filled = has_order_been_filled(ID)
    #     time.sleep(1.1) # time limit
    # print("Order has been filled")
