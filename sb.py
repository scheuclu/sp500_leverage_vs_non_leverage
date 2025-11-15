import os
from postgrest import APIResponse

from models import (
    Position,
)
from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

TRADING212_KEY = os.environ["TRADING212_KEY"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]


def write_positions(positions: list[Position]) -> APIResponse:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    response = (
        supabase.table("data")
        .insert({"positions": [p.model_dump_json() for p in positions]})
        .execute()
    )
    return response
