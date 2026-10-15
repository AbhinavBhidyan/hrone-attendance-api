"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
from contextlib import asynccontextmanager
import calendar
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
import os
from typing import Annotated, Any, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, field_validator, model_validator
import pymongo
from pymongo import MongoClient

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = ZoneInfo("Asia/Kolkata")


def init_db_indexes() -> None:
    """
    Create required MongoDB indexes idempotently at startup.
    - employees.emp_code (unique): unique business key constraint and fast employee lookups.
    - employees.(department, emp_code): optimizes department filtering and sorting by emp_code.
    - attendance_logs.(emp_code, date) (unique): unique natural key, race-safe punch-in constraint.
    - attendance_logs.(date desc, emp_code asc): powers listAttendance sort & range filter without COLLSCAN.
    - attendance_logs.(emp_code, punch_in desc): powers punch-out lookup for latest open record.
    """
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index([("joined_on", 1)])

    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db_indexes()
    yield
    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Validation & Schemas (OpenAPI Contract)
# --------------------------------------------------------------------------- #
# EpochMillis: strict integer milliseconds; rejects strings, floats, and seconds-looking values (< 100000000000)
EpochMillis = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]


class StatusEnum(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatusEnum(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            datetime.fromisoformat(v).date()
        except ValueError:
            raise ValueError("joined_on must be a valid date in YYYY-MM-DD format")
        return v

    @model_validator(mode="after")
    def validate_shift_times(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


# Backward-compatible alias for existing starter endpoints
EmployeeIn = EmployeeCreate


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatusEnum = PresenceStatusEnum.PRESENT


# Backward-compatible alias for existing starter endpoints
PunchInIn = PunchInRequest


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    status: Optional[StatusEnum] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Time, Serialization & Truncation Helpers
# --------------------------------------------------------------------------- #
def truncate_to_seconds_ms(epoch_ms: int) -> int:
    """R1: Instants are truncated to whole seconds before any calculation."""
    return (epoch_ms // 1000) * 1000


def epoch_ms_to_datetime(epoch_ms: int) -> datetime:
    """Convert integer epoch milliseconds to UTC datetime for MongoDB BSON storage."""
    return datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert BSON datetime from MongoDB to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def serialize_history_entry(entry: dict) -> dict:
    """Serialize a single history entry, ensuring BSON datetimes convert to epoch ms."""
    at_val = entry.get("at")
    at_ms = datetime_to_epoch_ms(at_val) if isinstance(at_val, datetime) else at_val

    raw_changes = entry.get("changes", {})
    changes = {}
    for field, change in raw_changes.items():
        if not isinstance(change, dict):
            continue
        c_from = change.get("from")
        c_to = change.get("to")
        if field in ("punch_in", "punch_out"):
            if isinstance(c_from, datetime):
                c_from = datetime_to_epoch_ms(c_from)
            if isinstance(c_to, datetime):
                c_to = datetime_to_epoch_ms(c_to)
        changes[field] = {"from": c_from, "to": c_to}

    return {
        "at": at_ms,
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def serialize_attendance_doc(doc: dict) -> dict:
    """Serialize MongoDB attendance document to API AttendanceRecord schema."""
    return {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")) if isinstance(doc.get("punch_in"), datetime) else doc.get("punch_in"),
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")) if isinstance(doc.get("punch_out"), datetime) else doc.get("punch_out"),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [serialize_history_entry(h) for h in doc.get("history") or []],
    }


def serialize_employee_doc(doc: dict) -> dict:
    """Serialize MongoDB employee document to API Employee schema."""
    created_at = doc.get("created_at")
    created_at_ms = datetime_to_epoch_ms(created_at) if isinstance(created_at, datetime) else created_at
    return {
        "emp_code": doc.get("emp_code"),
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": created_at_ms,
    }


# --------------------------------------------------------------------------- #
# Business Rule Helpers (R1 - R5, R8)
# --------------------------------------------------------------------------- #
def get_shift_datetimes(date_str: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """
    R1: Given an attendance date (YYYY-MM-DD) and shift times (HH:MM in IST),
    return (shift_start_dt, shift_end_dt) as timezone-aware IST datetimes.
    Overnight shift rule: if shift_end <= shift_start, shift_end falls on the next calendar day.
    """
    d = datetime.fromisoformat(date_str).date()
    sh_start_h, sh_start_m = map(int, shift_start.split(":"))
    sh_end_h, sh_end_m = map(int, shift_end.split(":"))

    start_dt = datetime(d.year, d.month, d.day, sh_start_h, sh_start_m, 0, tzinfo=IST)
    if (sh_end_h, sh_end_m) <= (sh_start_h, sh_start_m):
        end_date = d + timedelta(days=1)
    else:
        end_date = d
    end_dt = datetime(end_date.year, end_date.month, end_date.day, sh_end_h, sh_end_m, 0, tzinfo=IST)
    return start_dt, end_dt


def get_attendance_date(punch_in_ms: int, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance date determination. A record's date is the IST calendar date of punch_in.
    Overnight shift rule (shift_end <= shift_start, e.g. 22:00-06:00):
    a punch-in earlier than shift_end belongs to the shift that started the previous calendar day.
    """
    trunc_ms = truncate_to_seconds_ms(punch_in_ms)
    dt_ist = datetime.fromtimestamp(trunc_ms / 1000.0, tz=timezone.utc).astimezone(IST)

    sh_start_h, sh_start_m = map(int, shift_start.split(":"))
    sh_end_h, sh_end_m = map(int, shift_end.split(":"))
    is_overnight = (sh_end_h, sh_end_m) <= (sh_start_h, sh_start_m)

    if is_overnight and (dt_ist.hour, dt_ist.minute, dt_ist.second) < (sh_end_h, sh_end_m, 0):
        shift_date = dt_ist.date() - timedelta(days=1)
    else:
        shift_date = dt_ist.date()

    return shift_date.isoformat()


def compute_late_minutes(
    punch_in: int | datetime,
    shift_start: str,
    shift_end: str = "18:30",
    date_str: Optional[str] = None,
) -> int:
    """
    R2: Grace period 10 minutes. Late only if punch-in is strictly more than 10:00 (600s)
    after shift_start; then late_minutes = whole minutes (floor) elapsed since shift_start.
    Otherwise 0.
    """
    if isinstance(punch_in, datetime):
        if punch_in.tzinfo is None:
            punch_in = punch_in.replace(tzinfo=IST)
        punch_in_ms = int(punch_in.timestamp() * 1000)
    else:
        punch_in_ms = punch_in

    trunc_ms = truncate_to_seconds_ms(punch_in_ms)
    in_dt = datetime.fromtimestamp(trunc_ms / 1000.0, tz=timezone.utc).astimezone(IST)

    if date_str is None:
        date_str = get_attendance_date(trunc_ms, shift_start, shift_end)

    start_dt, _ = get_shift_datetimes(date_str, shift_start, shift_end)
    diff_sec = int((in_dt - start_dt).total_seconds())
    if diff_sec > 600:
        return diff_sec // 60
    return 0


def compute_overtime_minutes(
    punch_out: int | datetime,
    shift_start: str = "09:30",
    shift_end: str = "18:30",
    date_str: Optional[str] = None,
) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if at least 30;
    otherwise 0. Correctly accounts for overnight shifts ending on the next calendar day.
    """
    if isinstance(punch_out, datetime):
        if punch_out.tzinfo is None:
            punch_out = punch_out.replace(tzinfo=IST)
        punch_out_ms = int(punch_out.timestamp() * 1000)
    else:
        punch_out_ms = punch_out

    trunc_ms = truncate_to_seconds_ms(punch_out_ms)
    out_dt = datetime.fromtimestamp(trunc_ms / 1000.0, tz=timezone.utc).astimezone(IST)

    if date_str is None:
        date_str = out_dt.date().isoformat()

    _, end_dt = get_shift_datetimes(date_str, shift_start, shift_end)
    diff_sec = int((out_dt - end_dt).total_seconds())
    diff_min = diff_sec // 60
    return diff_min if diff_min >= 30 else 0


# Backward-compatible alias for existing starter endpoint
compute_overtime = compute_overtime_minutes


def compute_work_hours(punch_in: int | datetime, punch_out: int | datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up
    using Decimal and ROUND_HALF_UP (not banker's rounding).
    """
    in_ms = int(punch_in.timestamp() * 1000) if isinstance(punch_in, datetime) else punch_in
    out_ms = int(punch_out.timestamp() * 1000) if isinstance(punch_out, datetime) else punch_out

    trunc_in = truncate_to_seconds_ms(in_ms)
    trunc_out = truncate_to_seconds_ms(out_ms)

    diff_sec = (trunc_out - trunc_in) // 1000
    hours = Decimal(diff_sec) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: After punch-out, half_day is true when the rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def round_half_up(value: float | Decimal, decimals: int = 2) -> float:
    """
    R8: Standard half-up rounding (2 decimals for general numbers, 4 decimals for rates).
    """
    d_val = Decimal(str(value))
    target = Decimal("1" if decimals == 0 else f"0.{'0' * (decimals - 1)}1")
    return float(d_val.quantize(target, rounding=ROUND_HALF_UP))


def count_working_days(joined_on_str: str, month_str: str) -> int:
    """
    R7: Working days Mon-Fri, counted from joined_on for mid-month joiners.
    Returns 0 if joined_on is after the month.
    """
    year, month = map(int, month_str.split("-"))
    num_days = calendar.monthrange(year, month)[1]
    first_day = datetime(year, month, 1).date()
    last_day = datetime(year, month, num_days).date()
    joined_date = datetime.fromisoformat(joined_on_str).date()

    if joined_date > last_day:
        return 0

    start_date = max(first_day, joined_date)
    working_days = 0
    cur = start_date
    while cur <= last_day:
        if cur.weekday() < 5:  # Monday is 0, Friday is 4
            working_days += 1
        cur += timedelta(days=1)
    return working_days


# --------------------------------------------------------------------------- #
# Core Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """Returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    """
    Create an employee.
    Enforced by database unique index on emp_code (concurrent creates yield 1x 201 and 409 for rest).
    """
    now_ms = truncate_to_seconds_ms(int(datetime.now(timezone.utc).timestamp() * 1000))
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": epoch_ms_to_datetime(now_ms),
    }
    try:
        db.employees.insert_one(doc)
    except pymongo.errors.DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    List employees sorted by emp_code ascending.
    total counts the filtered set, not the whole collection.
    """
    q: dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    """
    Record a punch-in.
    Creates the day's record per R1 and R2.
    Race-safe: two simultaneous punch-ins for the same emp_code and date yield exactly one 201 and the other 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        raw_ms = body.punched_at
    else:
        raw_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    punch_in_ms = truncate_to_seconds_ms(raw_ms)
    date_str = get_attendance_date(punch_in_ms, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_ms, emp["shift_start"], emp["shift_end"], date_str)
    status_val = body.status.value if isinstance(body.status, Enum) else body.status

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": status_val,
        "punch_in": epoch_ms_to_datetime(punch_in_ms),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except pymongo.errors.DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this date")

    return serialize_attendance_doc(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusEnum] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    List attendance records sorted by date descending, then emp_code ascending.
    date_from/date_to are inclusive; date_from > date_to -> 422.
    """
    if date_from:
        try:
            datetime.fromisoformat(date_from).date()
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date_from format, expected YYYY-MM-DD")
    if date_to:
        try:
            datetime.fromisoformat(date_to).date()
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date_to format, expected YYYY-MM-DD")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not be greater than date_to")

    q: dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status.value if isinstance(status, Enum) else status
    if date_from or date_to:
        date_filter: dict[str, str] = {}
        if date_from:
            date_filter["$gte"] = date_from
        if date_to:
            date_filter["$lte"] = date_to
        q["date"] = date_filter

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [serialize_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    """
    Record a punch-out.
    Closes employee's most recent record whose punch_in <= punched_at (works across midnight).
    Computes work_hours (R4), overtime_minutes (R3), and half_day (R5).
    Two simultaneous punch-outs yield exactly one 200 and one 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        raw_ms = body.punched_at
    else:
        raw_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    punch_out_ms = truncate_to_seconds_ms(raw_ms)
    punch_out_dt = epoch_ms_to_datetime(punch_out_ms)

    # Find the most recent record whose punch_in <= punch_out_dt
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", -1)],
    )
    if not record:
        # Check if the employee has a punch_in record after punched_at (422 if punched_at <= punch_in)
        subsequent_record = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
            sort=[("punch_in", -1)],
        )
        if subsequent_record and subsequent_record.get("punch_in"):
            in_ms = datetime_to_epoch_ms(subsequent_record["punch_in"])
            if in_ms is not None and punch_out_ms <= in_ms:
                raise HTTPException(status_code=422, detail="punched_at must be strictly after punch_in")
        raise HTTPException(status_code=404, detail="No punch-in record found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    punch_in_ms = datetime_to_epoch_ms(record.get("punch_in"))
    if punch_in_ms is None:
        raise HTTPException(status_code=422, detail="Record has no punch-in")

    if punch_out_ms <= punch_in_ms:
        raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")

    if punch_out_ms - punch_in_ms > 24 * 3600 * 1000:
        raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    work_hours = compute_work_hours(punch_in_ms, punch_out_ms)
    overtime_min = compute_overtime_minutes(punch_out_ms, emp["shift_start"], emp["shift_end"], record["date"])
    half_day = compute_half_day(work_hours)

    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_min,
                "half_day": half_day,
            }
        },
        return_document=pymongo.ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Record was already punched out")

    return serialize_attendance_doc(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    """
    Regularize (manually correct) one day's record.
    Recomputes late_minutes, work_hours, overtime_minutes, half_day from final times.
    Appends one entry to history containing only fields that actually changed.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="No attendance record found for this employee and date")

    # Status ABSENT or LEAVE clears both punch times; supplying punch_in or punch_out with it is 422
    if body.status in (StatusEnum.ABSENT, StatusEnum.LEAVE):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="Cannot provide punch_in or punch_out when setting status to ABSENT or LEAVE")

    target_status = body.status.value if body.status is not None else record["status"]

    if target_status in ("PRESENT", "WFH", "ON_DUTY"):
        # Presence status requires punch_in
        if body.punch_in is not None:
            target_punch_in_ms = truncate_to_seconds_ms(body.punch_in)
        else:
            target_punch_in_ms = datetime_to_epoch_ms(record.get("punch_in"))

        if target_punch_in_ms is None:
            raise HTTPException(status_code=422, detail="A presence status requires a punch_in")

        # punch_in must stay on record's attendance date (R1)
        computed_date = get_attendance_date(target_punch_in_ms, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(status_code=422, detail="punch_in does not belong to the record's attendance date")

        # punch_out
        if body.punch_out is not None:
            target_punch_out_ms = truncate_to_seconds_ms(body.punch_out)
        else:
            target_punch_out_ms = datetime_to_epoch_ms(record.get("punch_out"))

        if target_punch_out_ms is not None:
            if target_punch_out_ms <= target_punch_in_ms:
                raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")
            if target_punch_out_ms - target_punch_in_ms > 24 * 3600 * 1000:
                raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

        # Recompute derived fields for presence
        new_late_minutes = compute_late_minutes(target_punch_in_ms, emp["shift_start"], emp["shift_end"], date)
        if target_punch_out_ms is not None:
            new_work_hours = compute_work_hours(target_punch_in_ms, target_punch_out_ms)
            new_overtime_minutes = compute_overtime_minutes(target_punch_out_ms, emp["shift_start"], emp["shift_end"], date)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False
    else:
        # ABSENT or LEAVE
        target_punch_in_ms = None
        target_punch_out_ms = None
        new_late_minutes = 0
        new_work_hours = None
        new_overtime_minutes = 0
        new_half_day = False

    # Old values
    old_status = record.get("status")
    old_punch_in_ms = datetime_to_epoch_ms(record.get("punch_in"))
    old_punch_out_ms = datetime_to_epoch_ms(record.get("punch_out"))
    old_work_hours = record.get("work_hours")
    old_late_minutes = record.get("late_minutes", 0)
    old_overtime_minutes = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)

    # Detect actual changes
    mongo_changes: dict[str, Any] = {}

    if old_status != target_status:
        mongo_changes["status"] = {"from": old_status, "to": target_status}
    if old_punch_in_ms != target_punch_in_ms:
        mongo_changes["punch_in"] = {
            "from": record.get("punch_in"),
            "to": epoch_ms_to_datetime(target_punch_in_ms) if target_punch_in_ms is not None else None,
        }
    if old_punch_out_ms != target_punch_out_ms:
        mongo_changes["punch_out"] = {
            "from": record.get("punch_out"),
            "to": epoch_ms_to_datetime(target_punch_out_ms) if target_punch_out_ms is not None else None,
        }
    if old_work_hours != new_work_hours:
        mongo_changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if old_late_minutes != new_late_minutes:
        mongo_changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
    if old_overtime_minutes != new_overtime_minutes:
        mongo_changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}
    if old_half_day != new_half_day:
        mongo_changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not mongo_changes:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    now_ms = truncate_to_seconds_ms(int(datetime.now(timezone.utc).timestamp() * 1000))
    history_entry = {
        "at": epoch_ms_to_datetime(now_ms),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": mongo_changes,
    }

    # Atomic update with optimistic concurrency filter to avoid lost history
    filter_doc = {
        "_id": record["_id"],
        "status": record.get("status"),
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
    }
    update_doc = {
        "$set": {
            "status": target_status,
            "punch_in": epoch_ms_to_datetime(target_punch_in_ms) if target_punch_in_ms is not None else None,
            "punch_out": epoch_ms_to_datetime(target_punch_out_ms) if target_punch_out_ms is not None else None,
            "work_hours": new_work_hours,
            "late_minutes": new_late_minutes,
            "overtime_minutes": new_overtime_minutes,
            "half_day": new_half_day,
        },
        "$push": {
            "history": history_entry
        },
    }

    updated = db.attendance_logs.find_one_and_update(
        filter_doc,
        update_doc,
        return_document=pymongo.ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Record was concurrently modified")

    return serialize_attendance_doc(updated)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (MongoDB Aggregations)
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """
    Monthly attendance summary for one employee (backed by MongoDB aggregation).
    working_days per R7. present_days per R5-R7 (half day = 0.5, Mon-Fri only).
    attendance_pct = present_days / working_days x 100, R8 rounding, or null when working_days is 0.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, month_num = map(int, month.split("-"))
    num_days = calendar.monthrange(year, month_num)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    working_days = count_working_days(emp["joined_on"], month)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
            }
        },
        {
            "$addFields": {
                "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                "is_presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "is_leave": {"$eq": ["$status", "LEAVE"]},
                "is_late": {"$gt": ["$late_minutes", 0]},
            }
        },
        {
            "$addFields": {
                "present_contrib": {
                    "$cond": [
                        {"$and": ["$is_presence", {"$in": ["$dow", [2, 3, 4, 5, 6]]}]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                        0.0,
                    ]
                },
                "leave_contrib": {"$cond": ["$is_leave", 1, 0]},
                "late_contrib": {"$cond": ["$is_late", 1, 0]},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": "$present_contrib"},
                "leave_days": {"$sum": "$leave_contrib"},
                "late_count": {"$sum": "$late_contrib"},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        agg = res[0]
        present_days = float(agg["present_days"])
        leave_days = int(agg["leave_days"])
        late_count = int(agg["late_count"])
        total_late_minutes = int(agg["total_late_minutes"])
        total_overtime_minutes = int(agg["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """
    Department summary for a month (aggregation across employees and attendance_logs).
    headcount per R9 (people with zero logs still count, people who join later do not).
    avg_work_hours is the mean work_hours over records that are present-status and have work_hours.
    """
    year, month_num = map(int, month.split("-"))
    num_days = calendar.monthrange(year, month_num)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    match_filter: dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department is not None:
        match_filter["department"] = department

    pipeline = [
        {"$match": match_filter},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$addFields": {
                "emp_present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}}, [2, 3, 4, 5, 6]]},
                                        ]
                                    },
                                    {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "emp_work_hours_list": {
                    "$filter": {
                        "input": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$ne": ["$$l.work_hours", None]},
                                            ]
                                        },
                                        "$$l.work_hours",
                                        None,
                                    ]
                                },
                            }
                        },
                        "as": "wh",
                        "cond": {"$ne": ["$$wh", None]},
                    }
                },
                "emp_late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "emp_total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$ifNull": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "emp_leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "emp_on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_present_days"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"},
                "all_work_hours": {"$push": "$emp_work_hours_list"},
            }
        },
        {
            "$addFields": {
                "flat_work_hours": {
                    "$reduce": {
                        "input": "$all_work_hours",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$flat_work_hours"}, 0]},
                        {"$avg": "$flat_work_hours"},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"department": 1}},
    ]

    items = list(db.employees.aggregate(pipeline))
    for item in items:
        if item.get("avg_work_hours") is not None:
            item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Late-comers leaderboard for a month.
    Ranks employees by total late_minutes in the month (only total > 0), highest first.
    Standard competition ranking with ties (1, 2, 2, 4) using $rank.
    limit is applied after ranking.
    """
    year, month_num = map(int, month.split("-"))
    num_days = calendar.monthrange(year, month_num)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    pipeline: list[dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {
                        "$rank": {}
                    }
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    """
    Daily attendance trend for a department.
    Returns exactly one row per calendar day from from to to inclusive using $densify.
    moving_avg_7d is the mean of non-null attendance_rate values among current row and up to 6 preceding rows.
    """
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    try:
        d_from = datetime.fromisoformat(from_date).date()
        d_to = datetime.fromisoformat(to_date).date()
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format, expected YYYY-MM-DD")

    if d_to < d_from:
        raise HTTPException(status_code=422, detail="to must not be earlier than from")
    if (d_to - d_from).days > 92:
        raise HTTPException(status_code=422, detail="Date range must not exceed 92 days")

    from_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc)
    to_dt_plus_1 = datetime(d_to.year, d_to.month, d_to.day, tzinfo=timezone.utc) + timedelta(days=1)

    dept_emp_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1})]

    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": dept_emp_codes},
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                },
            }
        },
        {
            "$addFields": {
                "dt": {"$dateFromString": {"dateString": "$_id"}}
            }
        },
        {
            "$densify": {
                "field": "dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_1],
                },
            }
        },
        {
            "$addFields": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$dt"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "dow": {"$dayOfWeek": "$dt"},
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$in": ["$dow", [2, 3, 4, 5, 6]]}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$first": "$hc.count"}, 0]}
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]

    items = list(db.attendance_logs.aggregate(pipeline))
    for item in items:
        if item.get("attendance_rate") is not None:
            item["attendance_rate"] = round_half_up(item["attendance_rate"], 4)
        if item.get("moving_avg_7d") is not None:
            item["moving_avg_7d"] = round_half_up(item["moving_avg_7d"], 4)

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
class ExplainEndpointEnum(str, Enum):
    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainEndpointEnum = Path(..., description="Target endpoint to explain"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: Optional[int] = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusEnum] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    Runs explain with executionStats verbosity for the main query or aggregation
    of the named endpoint, built exactly as that endpoint builds it.
    """
    if endpoint == ExplainEndpointEnum.attendance_list:
        if date_from:
            try:
                datetime.fromisoformat(date_from).date()
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid date_from format, expected YYYY-MM-DD")
        if date_to:
            try:
                datetime.fromisoformat(date_to).date()
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid date_to format, expected YYYY-MM-DD")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must not be greater than date_to")

        q: dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status.value if isinstance(status, Enum) else status
        if date_from or date_to:
            date_filter: dict[str, str] = {}
            if date_from:
                date_filter["$gte"] = date_from
            if date_to:
                date_filter["$lte"] = date_to
            q["date"] = date_filter

        skip = (page - 1) * page_size
        cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        collection = "attendance_logs"

    elif endpoint == ExplainEndpointEnum.employee_monthly:
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required for employee_monthly")
        if not month:
            raise HTTPException(status_code=422, detail="month is required for employee_monthly")

        year, month_num = map(int, month.split("-"))
        num_days = calendar.monthrange(year, month_num)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{num_days:02d}"

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": start_date, "$lte": end_date},
                }
            },
            {
                "$addFields": {
                    "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                    "is_presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    "is_leave": {"$eq": ["$status", "LEAVE"]},
                    "is_late": {"$gt": ["$late_minutes", 0]},
                }
            },
            {
                "$addFields": {
                    "present_contrib": {
                        "$cond": [
                            {"$and": ["$is_presence", {"$in": ["$dow", [2, 3, 4, 5, 6]]}]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    },
                    "leave_contrib": {"$cond": ["$is_leave", 1, 0]},
                    "late_contrib": {"$cond": ["$is_late", 1, 0]},
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {"$sum": "$present_contrib"},
                    "leave_days": {"$sum": "$leave_contrib"},
                    "late_count": {"$sum": "$late_contrib"},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                }
            },
        ]
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        collection = "attendance_logs"

    elif endpoint == ExplainEndpointEnum.department_summary:
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")

        year, month_num = map(int, month.split("-"))
        num_days = calendar.monthrange(year, month_num)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{num_days:02d}"

        match_filter: dict[str, Any] = {"joined_on": {"$lte": end_date}}
        if department is not None:
            match_filter["department"] = department

        pipeline = [
            {"$match": match_filter},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$emp"]},
                                        {"$gte": ["$date", start_date]},
                                        {"$lte": ["$date", end_date]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {
                "$addFields": {
                    "emp_present_days": {
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}}, [2, 3, 4, 5, 6]]},
                                            ]
                                        },
                                        {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                        0.0,
                                    ]
                                },
                            }
                        }
                    },
                    "emp_work_hours_list": {
                        "$filter": {
                            "input": {
                                "$map": {
                                    "input": "$logs",
                                    "as": "l",
                                    "in": {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                    {"$ne": ["$$l.work_hours", None]},
                                                ]
                                            },
                                            "$$l.work_hours",
                                            None,
                                        ]
                                    },
                                }
                            },
                            "as": "wh",
                            "cond": {"$ne": ["$$wh", None]},
                        }
                    },
                    "emp_late_count": {
                        "$size": {
                            "$filter": {
                                "input": "$logs",
                                "as": "l",
                                "cond": {"$gt": ["$$l.late_minutes", 0]},
                            }
                        }
                    },
                    "emp_total_late_minutes": {
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {"$ifNull": ["$$l.late_minutes", 0]},
                            }
                        }
                    },
                    "emp_leave_count": {
                        "$size": {
                            "$filter": {
                                "input": "$logs",
                                "as": "l",
                                "cond": {"$eq": ["$$l.status", "LEAVE"]},
                            }
                        }
                    },
                    "emp_on_duty_count": {
                        "$size": {
                            "$filter": {
                                "input": "$logs",
                                "as": "l",
                                "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                            }
                        }
                    },
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$sum": 1},
                    "present_days": {"$sum": "$emp_present_days"},
                    "late_count": {"$sum": "$emp_late_count"},
                    "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
                    "leave_count": {"$sum": "$emp_leave_count"},
                    "on_duty_count": {"$sum": "$emp_on_duty_count"},
                    "all_work_hours": {"$push": "$emp_work_hours_list"},
                }
            },
            {
                "$addFields": {
                    "flat_work_hours": {
                        "$reduce": {
                            "input": "$all_work_hours",
                            "initialValue": [],
                            "in": {"$concatArrays": ["$$value", "$$this"]},
                        }
                    }
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "department": "$_id",
                    "headcount": 1,
                    "present_days": 1,
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "leave_count": 1,
                    "on_duty_count": 1,
                    "avg_work_hours": {
                        "$cond": [
                            {"$gt": [{"$size": "$flat_work_hours"}, 0]},
                            {"$avg": "$flat_work_hours"},
                            None,
                        ]
                    },
                }
            },
            {"$sort": {"department": 1}},
        ]
        cmd = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }
        collection = "employees"

    elif endpoint == ExplainEndpointEnum.late_leaderboard:
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")

        year, month_num = map(int, month.split("-"))
        num_days = calendar.monthrange(year, month_num)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{num_days:02d}"

        actual_limit = limit if limit is not None else 10

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": start_date, "$lte": end_date},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "emp",
                }
            },
            {"$unwind": "$emp"},
        ]

        if department is not None:
            pipeline.append({"$match": {"emp.department": department}})

        pipeline.extend([
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {
                        "rank": {
                            "$rank": {}
                        }
                    },
                }
            },
            {"$match": {"rank": {"$lte": actual_limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ])
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        collection = "attendance_logs"

    elif endpoint == ExplainEndpointEnum.department_trend:
        if not department:
            raise HTTPException(status_code=422, detail="department is required for department_trend")
        if not from_date:
            raise HTTPException(status_code=422, detail="from is required for department_trend")
        if not to_date:
            raise HTTPException(status_code=422, detail="to is required for department_trend")

        try:
            d_from = datetime.fromisoformat(from_date).date()
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid from format, expected YYYY-MM-DD")
        try:
            d_to = datetime.fromisoformat(to_date).date()
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid to format, expected YYYY-MM-DD")

        if d_to < d_from:
            raise HTTPException(status_code=422, detail="to must not be earlier than from")
        if (d_to - d_from).days > 92:
            raise HTTPException(status_code=422, detail="Date range must not exceed 92 days")

        from_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc)
        to_dt_plus_1 = datetime(d_to.year, d_to.month, d_to.day, tzinfo=timezone.utc) + timedelta(days=1)

        dept_emp_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1})]

        pipeline = [
            {
                "$match": {
                    "emp_code": {"$in": dept_emp_codes},
                    "date": {"$gte": from_date, "$lte": to_date},
                }
            },
            {
                "$group": {
                    "_id": "$date",
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                        }
                    },
                }
            },
            {
                "$addFields": {
                    "dt": {"$dateFromString": {"dateString": "$_id"}}
                }
            },
            {
                "$densify": {
                    "field": "dt",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [from_dt, to_dt_plus_1],
                    },
                }
            },
            {
                "$addFields": {
                    "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$dt"}},
                    "present_count": {"$ifNull": ["$present_count", 0.0]},
                    "late_count": {"$ifNull": ["$late_count", 0]},
                    "dow": {"$dayOfWeek": "$dt"},
                }
            },
            {
                "$addFields": {
                    "is_working_day": {"$in": ["$dow", [2, 3, 4, 5, 6]]}
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "let": {"cur_date": "$date"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$department", department]},
                                        {"$lte": ["$joined_on", "$$cur_date"]},
                                    ]
                                }
                            }
                        },
                        {"$count": "count"},
                    ],
                    "as": "hc",
                }
            },
            {
                "$addFields": {
                    "headcount": {"$ifNull": [{"$first": "$hc.count"}, 0]}
                }
            },
            {
                "$addFields": {
                    "attendance_rate": {
                        "$cond": [
                            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                            {"$divide": ["$present_count", "$headcount"]},
                            None,
                        ]
                    }
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"date": 1},
                    "output": {
                        "moving_avg_7d": {
                            "$avg": "$attendance_rate",
                            "window": {
                                "documents": [-6, 0]
                            },
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date": 1,
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": 1,
                    "moving_avg_7d": 1,
                }
            },
            {"$sort": {"date": 1}},
        ]
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        collection = "attendance_logs"

    else:
        raise HTTPException(status_code=422, detail=f"Unsupported endpoint selector: {endpoint}")

    explain_doc = db.command("explain", cmd, verbosity="executionStats")
    return {
        "endpoint": endpoint.value if isinstance(endpoint, Enum) else endpoint,
        "collection": collection,
        "explain": jsonable_encoder(explain_doc),
    }
