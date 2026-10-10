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
    return seconds // 60 if seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((as_utc(punch_out) - as_utc(punch_in)).total_seconds())
    value = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(value)


def compute_overtime(punch_out: datetime, shift_end: str, attendance_day: str, shift_start: str) -> int:
    day = date.fromisoformat(attendance_day)
    overnight = shift_end <= shift_start
    end = shift_datetime(day, shift_end, overnight_next_day=overnight)
    minutes = int((as_utc(punch_out) - end).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def round_half_up(value: Any, places: int = 2) -> Optional[float]:
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def month_bounds(month: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "month must use YYYY-MM")
    year, mon = map(int, month.split("-"))
    last = calendar.monthrange(year, mon)[1]
    return date(year, mon, 1), date(year, mon, last)


def weekdays_between(first: date, last: date) -> int:
    if last < first:
        return 0
    count = 0
    current = first
    while current <= last:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def validate_date_string(value: str, label: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        raise HTTPException(422, f"{label} must use YYYY-MM-DD")


# ------------------------------- serialization ------------------------------

def serialize_history(history: Optional[list]) -> list:
    result = []
    for entry in history or []:
        changes = {}
        for field, pair in (entry.get("changes") or {}).items():
            changes[field] = {
                "from": epoch_ms(pair.get("from")) if field in {"punch_in", "punch_out"} else pair.get("from"),
                "to": epoch_ms(pair.get("to")) if field in {"punch_in", "punch_out"} else pair.get("to"),
            }
        result.append({
            "at": epoch_ms(entry.get("at")),
            "by": entry.get("by"),
            "reason": entry.get("reason"),
            "changes": changes,
        })
    return result


def serialize_attendance(doc: dict) -> dict:
    return {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": epoch_ms(doc.get("punch_in")),
        "punch_out": epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": serialize_history(doc.get("history", [])),
    }


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": epoch_ms(doc["created_at"]),
    }


# --------------------------------- models -----------------------------------

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value):
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except ValueError:
            raise ValueError("joined_on must use YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def different_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None and not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
            raise ValueError("punched_at must be epoch milliseconds")
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None and not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
            raise ValueError("punched_at must be epoch milliseconds")
        return value


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def reject_null_instants(cls, data):
        if isinstance(data, dict):
            for key in ("punch_in", "punch_out"):
                if key in data and data[key] is None:
                    raise ValueError(f"{key} must be epoch milliseconds when supplied")
        return data

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None and not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
            raise ValueError("timestamp must be epoch milliseconds")
        return value


# ------------------------------ startup/indexes -----------------------------

@app.on_event("startup")
def create_indexes():
    # Idempotent. Unique indexes are the concurrency guarantees.
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="employee_joined")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date"
    )
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_emp")
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING)], name="attendance_date_status")
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", DESCENDING), ("emp_code", ASCENDING)],
                                    name="attendance_late_month")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="attendance_punchout")


# ---------------------------------- health ----------------------------------

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


# -------------------------------- employees ---------------------------------

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    docs = list(db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING)
                .skip((page - 1) * page_size).limit(page_size))
    return {
        "items": [serialize_employee({**doc, "created_at": doc.get("created_at")}) for doc in docs],
        "total": total, "page": page, "page_size": page_size,
    }


# -------------------------------- attendance --------------------------------

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(404, "Employee not found")
    stamp = parse_epoch_ms(body.punched_at) if body.punched_at is not None else now_utc()
    stamp = truncate_seconds(stamp)
    attendance_day = attendance_date(stamp, employee)
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_day,
        "status": body.status,
        "punch_in": stamp,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(stamp, employee["shift_start"], employee["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this attendance date")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(404, "Employee not found")
    stamp = truncate_seconds(parse_epoch_ms(body.punched_at) if body.punched_at is not None else now_utc())
    # Find the latest open record at or before the supplied punch-out instant.
    candidate = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": stamp}},
        sort=[("punch_in", DESCENDING)],
    )
    if not candidate:
        raise HTTPException(404, "No punch-in found")
    if candidate.get("punch_out") is not None:
        raise HTTPException(409, "This attendance record is already punched out")
    punch_in_time = as_utc(candidate["punch_in"])
    if stamp <= punch_in_time or stamp > punch_in_time + timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    work_hours = compute_work_hours(punch_in_time, stamp)
    overtime = compute_overtime(stamp, employee["shift_end"], candidate["date"], employee["shift_start"])
    half_day = work_hours < 4.50
    updated = db.attendance_logs.find_one_and_update(
        {"_id": candidate["_id"], "punch_out": None},
        {"$set": {"punch_out": stamp, "work_hours": work_hours,
                  "overtime_minutes": overtime, "half_day": half_day}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "This attendance record was already punched out")
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        if status not in STATUSES:
            raise HTTPException(422, "Invalid status")
        query["status"] = status
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            validate_date_string(date_from, "date_from")
            query["date"]["$gte"] = date_from
        if date_to is not None:
            validate_date_string(date_to, "date_to")
            query["date"]["$lte"] = date_to
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must not be after date_to")
    total = db.attendance_logs.count_documents(query)
    docs = list(db.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)])
                .skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize_attendance(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    day = validate_date_string(date)
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(404, "Employee not found")
    query = {"emp_code": emp_code, "date": day.isoformat()}
    old = db.attendance_logs.find_one(query)
    if not old:
        raise HTTPException(404, "Attendance record not found")

    new = dict(old)
    supplied = body.model_fields_set
    if "status" in supplied:
        new["status"] = body.status
    if "punch_in" in supplied:
        new["punch_in"] = parse_epoch_ms(body.punch_in)
    if "punch_out" in supplied:
        new["punch_out"] = parse_epoch_ms(body.punch_out)

    if new["status"] in {"ABSENT", "LEAVE"}:
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(422, "ABSENT and LEAVE cannot be supplied with punch times")
        new["punch_in"] = None
        new["punch_out"] = None
    elif new["status"] in PRESENCE and new.get("punch_in") is None:
        raise HTTPException(422, "A presence status requires punch_in")

    if new.get("punch_in") is not None:
        if attendance_date(new["punch_in"], employee) != day.isoformat():
            raise HTTPException(422, "punch_in must belong to the attendance date")
        new["late_minutes"] = compute_late_minutes(new["punch_in"], employee["shift_start"], employee["shift_end"])
    else:
        new["late_minutes"] = 0

    if new.get("punch_out") is not None:
        if new.get("punch_in") is None or as_utc(new["punch_out"]) <= as_utc(new["punch_in"]):
            raise HTTPException(422, "punch_out must be after punch_in")
        if as_utc(new["punch_out"]) > as_utc(new["punch_in"]) + timedelta(hours=24):
            raise HTTPException(422, "punch_out must be within 24 hours of punch_in")
        new["work_hours"] = compute_work_hours(new["punch_in"], new["punch_out"])
        new["overtime_minutes"] = compute_overtime(
            new["punch_out"], employee["shift_end"], day.isoformat(), employee["shift_start"]
        )
        new["half_day"] = new["work_hours"] < 4.50
    else:
        new["work_hours"] = None
        new["overtime_minutes"] = 0
        new["half_day"] = False

    if new["status"] in {"ABSENT", "LEAVE"}:
        new.update({"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False})

    changed = {}
    for field in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        before, after = old.get(field), new.get(field)
        if field in {"punch_in", "punch_out"}:
            before = as_utc(before) if before else None
            after = as_utc(after) if after else None
        if before != after:
            changed[field] = {"from": old.get(field), "to": new.get(field)}

    if not changed:
        raise HTTPException(422, "The request does not change the record")

    history_entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changed,
    }
    fields_to_set = {field: new.get(field) for field in
                     ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")}
    # Optimistic concurrency check ensures two edits cannot overwrite one another's audit trail.
    if "history" in old:
        history_guard = {"history": old["history"]}
    else:
        history_guard = {"history": {"$exists": False}}
    updated = db.attendance_logs.update_one(
        {"_id": old["_id"], **history_guard},
        {"$set": fields_to_set, "$push": {"history": history_entry}},
    )
    if updated.modified_count != 1:
        raise HTTPException(409, "Record changed concurrently; retry")
    result = db.attendance_logs.find_one(query)
    return serialize_attendance(result)


# ------------------------------- analytics ----------------------------------

def monthly_pipeline(emp_code: str, first: date, last: date) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": "$emp_code",
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", list(PRESENCE)]},
                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]},
                ]},
                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(404, "Employee not found")
    first, last = month_bounds(month)
    working_start = max(first, date.fromisoformat(employee["joined_on"]))
    working_days = weekdays_between(working_start, last)
    results = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, first, last)))
    result = results[0] if results else {
        "present_days": 0, "leave_days": 0, "late_count": 0,
        "total_late_minutes": 0, "total_overtime_minutes": 0,
    }
    present_days = round_half_up(result.get("present_days", 0))
    pct = round_half_up(Decimal(str(present_days)) / Decimal(working_days) * 100, 2) if working_days else None
    return {
        "emp_code": emp_code, "month": month, "working_days": working_days,
        "present_days": present_days, "leave_days": result.get("leave_days", 0),
        "late_count": result.get("late_count", 0),
        "total_late_minutes": result.get("total_late_minutes", 0),
        "total_overtime_minutes": result.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


def department_summary_pipeline(first: date, last: date, department: Optional[str] = None) -> list:
    match_employee = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        match_employee["department"] = department
    return [
        {"$match": match_employee},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code", "joined": "$joined_on"},
            "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$code"]},
                {"$gte": ["$date", first.isoformat()]},
                {"$lte": ["$date", last.isoformat()]},
                {"$gte": ["$date", "$$joined"]},
            ]}}}],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount_codes": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", list(PRESENCE)]},
                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", first.isoformat()]}}}}, [2, 3, 4, 5, 6]]},
                ]},
                {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]}, 0,
            ]}},
            "work_hours_values": {"$push": {"$cond": [
                {"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]},
                "$logs.work_hours", None,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {
            "_id": 0, "department": "$_id",
            "headcount": {"$size": "$headcount_codes"},
            "present_days": 1,
            "avg_work_hours": {"$avg": "$work_hours_values"},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    first, last = month_bounds(month)
    items = list(db.employees.aggregate(department_summary_pipeline(first, last, department)))
    for item in items:
        item["present_days"] = round_half_up(item.get("present_days", 0))
        item["avg_work_hours"] = round_half_up(item.get("avg_work_hours"))
    return {"month": month, "items": items}


def leaderboard_pipeline(first: date, last: date, limit: int, department: Optional[str] = None) -> list:
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name",
                      "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = month_bounds(month)
    return {"month": month, "items": list(db.attendance_logs.aggregate(
        leaderboard_pipeline(first, last, limit, department)
    ))}


def trend_pipeline(department: str, from_day: date, to_day: date) -> list:
    start_dt = datetime.combine(from_day, time.min, UTC)
    end_exclusive = datetime.combine(to_day + timedelta(days=1), time.min, UTC)
    # Seed both boundaries, densify between them, then look up actual daily data.
    return [
        {"$documents": [{"date": start_dt}, {"date": datetime.combine(to_day, time.min, UTC)}]},
        {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_exclusive]}}},
        {"$group": {"_id": "$date"}},
        {"$sort": {"_id": 1}},
        {"$set": {"date_string": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id", "timezone": "UTC"}}}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"day": "$date_string"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
                {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
                {"$unwind": "$employee"},
                {"$match": {"employee.department": department}},
                {"$group": {
                    "_id": None,
                    "present_count": {"$sum": {"$cond": [
                        {"$in": ["$status", list(PRESENCE)]},
                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0,
                    ]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                }},
            ],
            "as": "daily",
        }},
        {"$set": {
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$daily.present_count", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily.late_count", 0]}, 0]},
            "is_working_day": {"$in": [{"$dayOfWeek": "$_id"}, [2, 3, 4, 5, 6]]},
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"day": "$date_string"},
            "pipeline": [
                {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
                {"$count": "n"},
            ],
            "as": "hc",
        }},
        {"$set": {
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
            "attendance_rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": [{"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}, 0]}]},
                {"$divide": [{"$floor": [{"$add": [{"$multiply": [{"$divide": ["$present_count", {"$arrayElemAt": ["$hc.n", 0]}]}, 10000]}, 0.5]}]}, 10000]},
                None,
            ]},
        }},
        {"$setWindowFields": {"sortBy": {"_id": 1}, "output": {
            "moving_avg_raw": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}
        }}},
        {"$set": {"moving_avg_7d": {"$cond": [
            {"$eq": ["$moving_avg_raw", None]}, None,
            {"$divide": [{"$floor": [{"$add": [{"$multiply": ["$moving_avg_raw", 10000]}, 0.5]}]}, 10000]},
        ]}}},
        {"$project": {"_id": 0, "date": "$date_string", "is_working_day": 1, "headcount": 1,
                      "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_day: date = Query(..., alias="from"),
    to_day: date = Query(..., alias="to"),
):
    if to_day < from_day:
        raise HTTPException(422, "to must not be before from")
    if (to_day - from_day).days + 1 > 92:
        raise HTTPException(422, "Date range must not exceed 92 days")
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "Unknown department")
    pipeline = trend_pipeline(department, from_day, to_day)
    # Database-level aggregate supports the $documents stage, including a range with no attendance records.
    docs = list(db.aggregate(pipeline))
    for row in docs:
        row["attendance_rate"] = round_half_up(row.get("attendance_rate"), 4)
        row["moving_avg_7d"] = round_half_up(row.get("moving_avg_7d"), 4)
    return {"department": department, "items": docs}


# ------------------------------- explain plans ------------------------------

def explain_find(query: dict, sort: list, page: int, page_size: int) -> dict:
    return db.command("explain", {
        "find": "attendance_logs", "filter": query, "sort": dict(sort),
        "skip": (page - 1) * page_size, "limit": page_size,
    }, verbosity="executionStats")


def explain_aggregate(collection: str, pipeline: list) -> dict:
    return db.command("explain", {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
                      verbosity="executionStats")


def explain_query(endpoint: str, params: dict) -> tuple[str, dict]:
    if endpoint == "attendance_list":
        query = {}
        if params.get("emp_code") is not None:
            query["emp_code"] = params["emp_code"]
        if params.get("status") is not None:
            query["status"] = params["status"]
        if params.get("date_from") or params.get("date_to"):
            query["date"] = {}
            if params.get("date_from"):
                query["date"]["$gte"] = params["date_from"]
            if params.get("date_to"):
                query["date"]["$lte"] = params["date_to"]
        sort = [("date", DESCENDING), ("emp_code", ASCENDING)]
        return "attendance_logs", explain_find(query, sort, params["page"], params["page_size"])
    month = params.get("month")
    if endpoint in {"employee_monthly", "department_summary", "late_leaderboard"}:
        if not month:
            raise HTTPException(422, "month is required")
        first, last = month_bounds(month)
        if endpoint == "employee_monthly":
            if not params.get("emp_code"):
                raise HTTPException(422, "emp_code is required")
            return "attendance_logs", explain_aggregate(
                "attendance_logs", monthly_pipeline(params["emp_code"], first, last)
            )
        if endpoint == "department_summary":
            return "employees", explain_aggregate(
                "employees", department_summary_pipeline(first, last, params.get("department"))
            )
        return "attendance_logs", explain_aggregate(
            "attendance_logs", leaderboard_pipeline(first, last, params["limit"], params.get("department"))
        )
    if endpoint == "department_trend":
        department, start, end = params.get("department"), params.get("from"), params.get("to")
        if not department or not start or not end:
            raise HTTPException(422, "department, from, and to are required")
        start_day = validate_date_string(start, "from")
        end_day = validate_date_string(end, "to")
        if end_day < start_day or (end_day - start_day).days + 1 > 92:
            raise HTTPException(422, "Invalid trend date range")
        return "attendance_logs", db.command(
            "explain", {"aggregate": 1, "pipeline": trend_pipeline(department, start_day, end_day), "cursor": {}},
            verbosity="executionStats",
        )
    raise HTTPException(422, "Unsupported endpoint")


@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[str] = None,
    from_day: Optional[date] = Query(None, alias="from"),
    to_day: Optional[date] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed:
        raise HTTPException(422, "Unsupported endpoint")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    params = {
        "emp_code": emp_code, "month": month, "department": department, "limit": limit,
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
        "status": status, "from": from_day.isoformat() if from_day else None,
        "to": to_day.isoformat() if to_day else None, "page": page, "page_size": page_size,
    }
    collection, explain = explain_query(endpoint, params)
    # Convert BSON-specific values to ordinary JSON-compatible data.
    return {"endpoint": endpoint, "collection": collection, "explain": json_util.loads(json_util.dumps(explain))}

