import os
from datetime import datetime

from dotenv import load_dotenv
from supabase import Client, create_client

from sp500_bot.models import Position

load_dotenv()

SUPABASE_KEY = os.environ["SUPABASE_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]

_supabase: Client | None = None


def _get_client() -> Client:
    """Get or create Supabase client (singleton)."""
    global _supabase
    if _supabase is None:
        _supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase


def write_positions(positions: list[Position]) -> None:
    """Write position data to the 'data' table."""
    client = _get_client()
    client.table("data").insert(
        {"positions": [p.model_dump_json() for p in positions]}
    ).execute()


def write_state(
    state_name: str,
    time_last_base_change: datetime,
    base_value_at_last_change: float,
    lev_value_at_last_change: float,
) -> None:
    """Write trader state to the 'state' table."""
    client = _get_client()
    client.table("state").insert(
        {
            "state_name": state_name,
            "time_last_base_change": time_last_base_change.isoformat(),
            "base_value_at_last_change": base_value_at_last_change,
            "lev_value_at_last_change": lev_value_at_last_change,
        }
    ).execute()
