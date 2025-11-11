
import requests
from jinja2.nodes import Literal

from models import Order
from dotenv import load_dotenv
load_dotenv()
import os
import time
from pydantic import BaseModel, Field

TRADING212_KEY = os.environ["TRADING212_KEY"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]


class ToolError(BaseModel):
    """Whenever this is returned, it means something went wront when calling a tool and we are capturing the problem here."""

    error_type: str = Field(description="Type of the error that has occured.")
    message: str = Field(description="A message describing what exactly the issue is.")

from enum import Enum
class Trading212Ticker(Enum):
    """Ticker string used for all trading212 endpoints"""
    SP500_ACC = "VUAG"



def cancel_order_by_id(id: int) -> bool | ToolError:
    try:
        url = "https://demo.trading212.com/api/v0/equity/orders/" + str(id)

        headers = {
            "Authorization": TRADING212_KEY,
            # "Content-Type": "application/json",
        }

        response = requests.delete(url, headers=headers)
        response.raise_for_status()
        return response.status_code == 200
    except Exception as e:
        return ToolError(message=str(e), error_type="RequestError")


def fetch_open_orders() -> list[Order] | ToolError:
    try:
        url = "https://demo.trading212.com/api/v0/equity/orders"

        headers = {
            "Authorization": TRADING212_KEY,
            "Content-Type": "application/json",
        }

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

        headers = {
            "Authorization": TRADING212_KEY,
            "Content-Type": "application/json",
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

        headers = {
            "Content-Type": "application/json",
            "Authorization": TRADING212_KEY,
        }

        response = requests.post(url, json=payload, headers=headers)

        data = response.json()
        return Order(**data)
    except Exception as e:
        return ToolError(message=str(e), error_type="RequestError")


class LimitOrder(BaseModel):

    ticker: Trading212Ticker
    quantity: float =Field(gt=0.0)
    limit_price: float = Field(gt=0.0)
    type: Literal["sell", "buy"]


def place_limit_order(order: LimitOrder) -> Order | ToolError:
    """Selling an asset with specific ticker on Trading212.

    The `quantity` needs to be negative.
    """
    try:
        url = "https://demo.trading212.com/api/v0/equity/orders/stop"

        payload = {
            "quantity": order.quantity,  # 0.01
            "limitPrice": order.limit_price,  # 2960
            "ticker": order.ticker,
            "timeValidity": "DAY",
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": TRADING212_KEY,
        }

        response = requests.post(url, json=payload, headers=headers)

        data = response.json()
        return Order(**data)
    except Exception as e:
        return ToolError(message=str(e), error_type="RequestError")


if __name__ == "__main__":
    # open_orders = fetch_open_orders()
    cancel_open_orders()
