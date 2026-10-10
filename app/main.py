"""
Employee Attendance & Analytics API.
Run from the repository root: uvicorn app.main:app --port 8000
"""
import calendar
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import json_util

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ------------------------------- time helpers -------------------------------

def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def as_utc(value: datetime) -> datetime:
    """PyMongo returns naive BSON datetimes; they represent UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def truncate_seconds(value: datetime) -> datetime:
    return as_utc(value).replace(microsecond=0)


def epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    value = as_utc(value)
    return int(value.timestamp() * 1000)


def parse_epoch_ms(value: int) -> datetime:
    if type(value) is not int or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise HTTPException(422, "timestamp must be an integer epoch-millisecond value in range")
    try:
        return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)
    except (OverflowError, OSError, ValueError):
        raise HTTPException(422, "timestamp is out of range")


def local_time(value: datetime) -> datetime:
    return as_utc(value).astimezone(IST)


def shift_datetime(day: date, hhmm: str, overnight_next_day: bool = False) -> datetime:
    hour, minute = map(int, hhmm.split(":"))
    shift_day = day + timedelta(days=1) if overnight_next_day else day
    return datetime.combine(shift_day, time(hour, minute), IST).astimezone(UTC).replace(microsecond=0)


def attendance_date(punch_in: datetime, employee: dict) -> str:
    local = local_time(punch_in)
    shift_start = employee["shift_start"]
    shift_end = employee["shift_end"]
    if shift_end <= shift_start and local.strftime("%H:%M") < shift_end:
        return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str) -> int:
    local = local_time(punch_in)
    shift_day = local.date()
    if shift_end <= shift_start and local.strftime("%H:%M") < shift_end:
        shift_day -= timedelta(days=1)
    hour, minute = map(int, shift_start.split(":"))
    start = datetime.combine(shift_day, time(hour, minute), IST)
    seconds = int((local - start).total_seconds())
