from sp500_bot.models import (
    TradableInstrument,
    Exchange,
    WorkingSchedule,
    Position,
    TimeEvent,
    Type3,
)

import datetime


def is_exchange_open(timeEvents: list[TimeEvent] | None):
    if not timeEvents:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    last_event = [t for t in timeEvents if t.date < now][-1]  # type: ignore
    return last_event.type == Type3.OPEN


def get_working_schedules(exchanges: list[Exchange]) -> dict[int, WorkingSchedule]:
    workingSchedules: dict[int, WorkingSchedule] = {}
    for exchange in exchanges:
        for w in exchange.workingSchedules:
            id = w.id
            workingSchedules[id] = w
    return workingSchedules


def are_positions_tradeable(
    exchanges: list[Exchange],
    instruments: list[TradableInstrument],
    positions: list[Position],
) -> bool:
    working_schedule_ids = [instruments[p.ticker].workingScheduleId for p in positions]
    workingSchedules: dict[int, WorkingSchedule] = get_working_schedules(exchanges=exchanges)
    ws = [workingSchedules[id] for id in working_schedule_ids]
    return all([is_exchange_open(w.timeEvents) for w in ws])
