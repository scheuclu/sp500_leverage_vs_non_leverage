import requests
from models import Order, Position, Cash, TradableInstrument, Exchange
from dotenv import load_dotenv

load_dotenv()
import os
from pydantic import BaseModel, Field

TRADING212_KEY = os.environ["TRADING212_KEY"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]

headers = {
    "Authorization": TRADING212_KEY,
    "Content-Type": "application/json",
}


class ToolError(BaseModel):
    """Whenever this is returned, it means something went wront when calling a tool and we are capturing the problem here."""

    error_type: str = Field(description="Type of the error that has occured.")
    message: str = Field(description="A message describing what exactly the issue is.")


from enum import Enum


class Trading212Ticker(Enum):
    """Ticker string used for all trading212 endpoints"""

    SP500_ACC = "VUAGl_EQ"
    SP500_5L = "5LUSl_EQ"


def cancel_order_by_id(id: int) -> bool:
    url = "https://demo.trading212.com/api/v0/equity/orders/" + str(id)

    response = requests.delete(url, headers=headers)
    response.raise_for_status()
    return response.status_code == 200


def fetch_open_orders() -> list[Order] | ToolError:
    try:
        url = "https://demo.trading212.com/api/v0/equity/orders"

        response = requests.get(url, headers=headers)
        response.raise_for_status()

        data = response.json()
        return [Order(**d) for d in data]
    except Exception as e:
        return ToolError(message=str(e), error_type="RequestError")


def cancel_open_orders():
    open_orders: list[Order] | ToolError = fetch_open_orders()
    assert isinstance(open_orders, list)
    for order in open_orders:
        if order.id:
            assert cancel_order_by_id(order.id)


def place_buy_order(ticker: Trading212Ticker, quantity: float) -> Order | ToolError:
    """Buying asset with specific ticker on Trading212"""

    try:
        url = "https://demo.trading212.com/api/v0/equity/orders/market"

        payload = {
            "quantity": quantity,
            "ticker": ticker.value,  # "AAPL_US_EQ"
        }

        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()

        data = response.json()
        return Order(**data)
    except Exception as e:
        return ToolError(message=str(e), error_type="RequestError")


def place_sell_order(
    ticker: Trading212Ticker, quantity: float, stop_price: float
) -> Order | ToolError:
    """Selling an asset with specific ticker on Trading212.

    The `quantity` needs to be negative.
    """
    try:
        url = "https://demo.trading212.com/api/v0/equity/orders/stop"

        payload = {
            "quantity": quantity,  # 0.01
            "stopPrice": stop_price,  # 2960
            "ticker": ticker.value,
            "timeValidity": "DAY",
        }

        response = requests.post(url, json=payload, headers=headers)

        data = response.json()
        return Order(**data)
    except Exception as e:
        return ToolError(message=str(e), error_type="RequestError")


def fetch_positions() -> list[Position]:
    url = "https://demo.trading212.com/api/v0/equity/portfolio"

    response = requests.get(url, headers=headers)
    response.raise_for_status()

    return [Position(**d) for d in response.json()]


def fetch_single_holding(ticker: Trading212Ticker) -> Position | None:
    url = "https://demo.trading212.com/api/v0/equity/portfolio/ticker"

    payload = {"ticker": ticker.value}

    response = requests.post(url, json=payload, headers=headers)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return Position(**response.json())


def fetch_account_cash() -> Cash:
    url = "https://demo.trading212.com/api/v0/equity/account/cash"

    response = requests.get(url, headers=headers)
    response.raise_for_status()

    return Cash(**response.json())


class LimitOrderType(Enum):
    BUY = 0
    SELL = 1


class LimitOrder(BaseModel):
    ticker: Trading212Ticker
    quantity: float = Field(gt=0.0)
    limit_price: float = Field(gt=0.0)
    type: LimitOrderType


def place_limit_order(order: LimitOrder) -> Order:
    """Selling means negative quantity"""
    url = "https://demo.trading212.com/api/v0/equity/orders/limit"

    payload = {
        "quantity": order.quantity
        if order.type == LimitOrderType.BUY
        else -order.quantity,  # 0.01
        "limitPrice": order.limit_price,  # 2960
        "ticker": order.ticker.value,
        "timeValidity": "DAY",
    }

    print(payload)

    response = requests.post(url, json=payload, headers=headers)
    response.raise_for_status()

    data = response.json()
    print(data)
    return Order(**data)


def fetch_open_order(id: int) -> Order | ToolError:
    try:
        url = f"https://demo.trading212.com/api/v0/equity/orders/{id}"

        response = requests.get(url, headers=headers)
        response.raise_for_status()

        data = response.json()
        return Order(**data)
    except Exception as e:
        return ToolError(message=str(e), error_type="RequestError")


def has_order_been_filled(id: int):
    url = f"https://demo.trading212.com/api/v0/equity/orders/{id}"

    response = requests.get(url, headers=headers)
    print(response)
    return response.status_code == 404


def fetch_instruments() -> list[TradableInstrument]:
    url = "https://demo.trading212.com/api/v0/equity/metadata/instruments"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    instruments = {d["ticker"]: TradableInstrument(**d) for d in response.json()}
    return instruments


def fetch_exchanges() -> list[Exchange]:
    url = "https://demo.trading212.com/api/v0/equity/metadata/exchanges"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    exchanges = [Exchange(**d) for d in response.json()]
    return exchanges


if __name__ == "__main__":
    result = fetch_single_holding(Trading212Ticker.AAPL)
    print(result)

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
