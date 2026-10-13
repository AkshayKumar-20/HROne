"""
Employee Attendance & Analytics API
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real env wins)

Single-file implementation. See openapi.yaml for the contract and business rules
R1-R10, and DATA_MODEL.md for what is stored in MongoDB.
"""
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone, date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # real environment variables take priority over .env

MONGO_URI = os.getenv("MONGO_URI") or "mongodb://localhost:27017"
MONGO_DB = os.getenv("MONGO_DB") or "attendance_db"

# tz_aware=True so PyMongo returns aware datetimes (UTC), never naive.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]
employees = db.employees
logs = db.attendance_logs

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH_MS_MIN = 100000000000        # ~2001-09-09; rejects seconds-looking values
EPOCH_MS_MAX = 4102444800000       # 2100-01-01
GRACE_MINUTES = 10
OVERTIME_MIN_MINUTES = 30
HALF_DAY_HOURS = Decimal("4.50")
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE")


# --------------------------------------------------------------------------- #
# Rounding / numeric helpers (R4, R8)
# --------------------------------------------------------------------------- #
def round_half_up(value, ndigits=2) -> float:
    """Round half-up (not banker's, not float) to ndigits. Returns a float."""
    q = Decimal(1).scaleb(-ndigits)  # 10^-ndigits
    return float(Decimal(repr(float(value))).quantize(q, rounding=ROUND_HALF_UP))


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Instant <-> epoch ms helpers (instants are integer epoch milliseconds)
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime (exact, no float division)."""
    return EPOCH + timedelta(milliseconds=int(ms))


def dt_to_ms(dt: datetime) -> int:
    """Aware/naive datetime (treated as UTC) -> epoch milliseconds (integer).

    Integer arithmetic only — no float total_seconds() precision loss.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = dt.astimezone(timezone.utc) - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def trunc_seconds_ms(ms: int) -> int:
    """Truncate an instant (epoch ms) to whole seconds, per R1."""
    return (int(ms) // 1000) * 1000


def now_ms() -> int:
    return trunc_seconds_ms(int(datetime.now(timezone.utc).timestamp() * 1000))


# --------------------------------------------------------------------------- #
# Shift / attendance-date helpers (R1, R2, R3, R5, R7)
# --------------------------------------------------------------------------- #
def _parse_hhmm(s: str):
    h, m = s.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start


def attendance_date_for(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """The IST calendar date a punch-in belongs to (R1), with overnight rule."""
    ist = punch_in_utc.astimezone(IST)
    d = ist.date()
    if is_overnight(shift_start, shift_end):
        eh, em = _parse_hhmm(shift_end)
        # A punch-in earlier than shift_end belongs to the shift that started the
        # previous day.
        if (ist.hour, ist.minute) < (eh, em):
            d = d - timedelta(days=1)
    return d.isoformat()


def shift_start_dt(attendance_date: date, shift_start: str) -> datetime:
    """UTC datetime of shift start on the given IST attendance date."""
    h, m = _parse_hhmm(shift_start)
    naive = datetime(attendance_date.year, attendance_date.month, attendance_date.day, h, m)
    return naive.replace(tzinfo=IST).astimezone(timezone.utc)


def shift_end_dt(attendance_date: date, shift_start: str, shift_end: str) -> datetime:
    """UTC datetime of shift end (next calendar day for overnight shifts, R1)."""
    h, m = _parse_hhmm(shift_end)
    d = attendance_date
    if is_overnight(shift_start, shift_end):
        d = d + timedelta(days=1)
    naive = datetime(d.year, d.month, d.day, h, m)
    return naive.replace(tzinfo=IST).astimezone(timezone.utc)


def compute_late_minutes(punch_in_ms: int, shift_start: str, attendance_date: date) -> int:
    """R2. Strictly more than 10:00 after shift start; floor minutes since shift start.

    Must compare seconds (not floored minutes): 09:40:00 -> 0 but 09:40:01 -> 10.
    punch_in_ms is already truncated to seconds (R1).
    """
    start_ms = dt_to_ms(shift_start_dt(attendance_date, shift_start))
    diff_ms = int(punch_in_ms) - start_ms
    if diff_ms <= GRACE_MINUTES * 60 * 1000:
        return 0
    return diff_ms // 60000


def compute_work_hours(punch_in_ms: int, punch_out_ms: int) -> float:
    """R4. (out-in)/3600 s, 2 decimals half-up."""
    secs = (punch_out_ms - punch_in_ms) / 1000.0
    return round_half_up(secs / 3600.0, 2)


def compute_overtime(punch_out_ms: int, shift_start: str, shift_end: str, attendance_date: date) -> int:
    """R3. Floor minutes from shift_end to punch_out; only if >= 30."""
    end = shift_end_dt(attendance_date, shift_start, shift_end)
    diff_min = (punch_out_ms - dt_to_ms(end)) // 60000
    return int(diff_min) if diff_min >= OVERTIME_MIN_MINUTES else 0


def compute_half_day(work_hours: float) -> bool:
    """R5. True when the ROUNDED work_hours < 4.50."""
    if work_hours is None:
        return False
    return Decimal(repr(work_hours)) < HALF_DAY_HOURS


def working_days_count(date_from: date, date_to: date, joined_on: date) -> int:
    """R7. Mon-Fri count between max(date_from, joined_on) and date_to inclusive."""
    start = max(date_from, joined_on)
    if start > date_to:
        return 0
    count = 0
    cur = start
    while cur <= date_to:
        if cur.weekday() < 5:  # Mon=0 .. Fri=4
            count += 1
        cur += timedelta(days=1)
    return count


def month_bounds(month: str):
    """'YYYY-MM' -> (first_day: date, last_day: date)."""
    y, m = int(month[:4]), int(month[5:7])
    first = date(y, m, 1)
    if m == 12:
        nxt = date(y + 1, 1, 1)
    else:
        nxt = date(y, m + 1, 1)
    return first, nxt - timedelta(days=1)


# --------------------------------------------------------------------------- #
# Pydantic models (contract-exact -> 422 on bad input)
# --------------------------------------------------------------------------- #
EMP_CODE_RE = r"^EMP\d{4,6}$"
EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
HHMM_RE = r"^([01]\d|2[0-3]):[0-5]\d$"
MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"
DATE_RE = r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"


def _validate_epoch_ms(v):
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("must be an integer of epoch milliseconds")
    if v < EPOCH_MS_MIN or v > EPOCH_MS_MAX:
        raise ValueError("out of range for epoch milliseconds")
    return v


class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=EMP_CODE_RE)
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=EMAIL_RE)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_RE)
    shift_end: str = Field(default="18:30", pattern=HHMM_RE)
    joined_on: str = Field(pattern=DATE_RE)

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v):
        date.fromisoformat(v)
        return v


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def _ms(cls, v):
        return _validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def _presence(cls, v):
        if v not in PRESENCE_STATUSES:
            raise ValueError("status must be PRESENT, WFH or ON_DUTY")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def _ms(cls, v):
        return _validate_epoch_ms(v)


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def _status(cls, v):
        if v is not None and v not in ALL_STATUSES:
            raise ValueError("invalid status")
        return v

    @field_validator("punch_in", "punch_out")
    @classmethod
    def _ms(cls, v):
        return _validate_epoch_ms(v)


# --------------------------------------------------------------------------- #
# App + startup (idempotent index creation)
# --------------------------------------------------------------------------- #
def create_indexes():
    # Unique business key: one employee per emp_code.
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    employees.create_index([("department", ASCENDING)], name="emp_department")
    employees.create_index([("joined_on", ASCENDING)], name="emp_joined_on")
    # Unique natural key for attendance: one record per (emp_code, date). This is
    # what makes concurrent punch-ins race-safe.
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_date")
    # List endpoint sort (date desc, emp_code asc) + filters.
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="att_date_emp")
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="att_emp_date_status")
    logs.create_index([("date", ASCENDING), ("status", ASCENDING)], name="att_date_status")


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# Kept for compatibility with older Starlette TestClient usage; lifespan is primary.
@app.on_event("startup")
def _startup_indexes():
    create_indexes()


# --------------------------------------------------------------------------- #
# Serialisation helpers (BSON datetime -> epoch ms at the boundary)
# --------------------------------------------------------------------------- #
def serialize_employee(doc) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]),
    }


def serialize_attendance(doc) -> dict:
    hist = []
    for h in (doc.get("history") or []):
        changes = {}
        for k, cv in (h.get("changes") or {}).items():
            frm, to = cv.get("from"), cv.get("to")
            if k in ("punch_in", "punch_out"):
                frm = dt_to_ms(frm) if frm is not None else None
                to = dt_to_ms(to) if to is not None else None
            changes[k] = {"from": frm, "to": to}
        hist.append({
            "at": dt_to_ms(h["at"]),
            "by": h["by"],
            "reason": h["reason"],
            "changes": changes,
        })
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc["punch_in"]) if doc.get("punch_in") else None,
        "punch_out": dt_to_ms(doc["punch_out"]) if doc.get("punch_out") else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day") or False),
        "history": hist,
    }


def _get_employee_or_404(emp_code: str):
    emp = employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "mongodb unavailable")


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start must differ from shift_end")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        employees.insert_one(doc)  # unique index enforces race-safety
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    total = employees.count_documents(q)  # total reflects the filter (R10)
    skip = (page - 1) * page_size         # off-by-one fixed
    cur = employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [serialize_employee(d) for d in cur]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = _get_employee_or_404(body.emp_code)
    raw_ms = body.punched_at if body.punched_at is not None else now_ms()
    punch_in_ms = trunc_seconds_ms(raw_ms)                      # R1 truncation
    punch_in_dt = ms_to_dt(punch_in_ms)
    date_str = attendance_date_for(punch_in_dt, emp["shift_start"], emp["shift_end"])
    att_date = date.fromisoformat(date_str)
    late = compute_late_minutes(punch_in_ms, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        logs.insert_one(doc)  # unique (emp_code, date) enforces race-safety
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = _get_employee_or_404(body.emp_code)
    raw_ms = body.punched_at if body.punched_at is not None else now_ms()
    punch_out_ms = trunc_seconds_ms(raw_ms)
    punch_out_dt = ms_to_dt(punch_out_ms)

    # Most recent open record whose punch_in <= punched_at (works across midnight).
    # Exclude ABSENT/LEAVE skeletons (punch_in null) — otherwise dt_to_ms(None) 500s.
    rec = logs.find_one(
        {"emp_code": body.emp_code, "punch_out": None,
         "punch_in": {"$ne": None, "$lte": punch_out_dt}},
        sort=[("punch_in", DESCENDING)],
    )
    if not rec or rec.get("punch_in") is None:
        raise HTTPException(404, "no open punch-in found")

    punch_in_dt = rec["punch_in"]
    punch_in_ms = dt_to_ms(punch_in_dt)
    if punch_out_ms <= punch_in_ms:
        raise HTTPException(422, "punched_at must be after punch_in")
    if punch_out_ms - punch_in_ms > 24 * 3600 * 1000:
        raise HTTPException(422, "punch-out is more than 24 hours after punch-in")

    att_date = date.fromisoformat(rec["date"])
    work_hours = compute_work_hours(punch_in_ms, punch_out_ms)
    overtime = compute_overtime(punch_out_ms, emp["shift_start"], emp["shift_end"], att_date)
    half_day = compute_half_day(work_hours)

    # Conditional update closes the record only if it is still open -> race-safe.
    res = logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {
            "punch_out": punch_out_dt,
            "work_hours": work_hours,
            "overtime_minutes": overtime,
            "half_day": half_day,
        }},
        return_document=ReturnDocument.AFTER,
    )
    if not res:
        raise HTTPException(409, "record already punched out")
    return serialize_attendance(res)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    try:
        if date_from:
            date.fromisoformat(date_from)
        if date_to:
            date.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(422, "invalid date, expected YYYY-MM-DD")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    if status is not None and status not in ALL_STATUSES:
        raise HTTPException(422, "invalid status")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = logs.count_documents(q)
    skip = (page - 1) * page_size
    cur = logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    items = [serialize_attendance(d) for d in cur]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# PATCH /attendance/{emp_code}/{date}  (regularize)
# --------------------------------------------------------------------------- #
@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date_str: str = Path(..., alias="date", pattern=DATE_RE),
    body: RegularizeRequest = ...,
):
    emp = _get_employee_or_404(emp_code)
    try:
        att_date = date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(422, "invalid date, expected YYYY-MM-DD")
    rec = logs.find_one({"emp_code": emp_code, "date": date_str})
    if not rec:
        raise HTTPException(404, "no attendance record for that date")

    new_status = body.status if body.status is not None else rec["status"]
    new_in_ms = trunc_seconds_ms(body.punch_in) if body.punch_in is not None else (
        dt_to_ms(rec["punch_in"]) if rec.get("punch_in") else None)
    new_out_ms = trunc_seconds_ms(body.punch_out) if body.punch_out is not None else (
        dt_to_ms(rec["punch_out"]) if rec.get("punch_out") else None)

    # Business-rule validation.
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "cannot supply punch times with ABSENT/LEAVE")
        new_in_ms, new_out_ms = None, None
    else:
        if new_in_ms is None:
            raise HTTPException(422, "a presence status requires punch_in")
        if attendance_date_for(ms_to_dt(new_in_ms), emp["shift_start"], emp["shift_end"]) != date_str:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
    if new_in_ms is not None and new_out_ms is not None:
        if new_out_ms <= new_in_ms:
            raise HTTPException(422, "punch_out must be after punch_in")
        if new_out_ms - new_in_ms > 24 * 3600 * 1000:
            raise HTTPException(422, "punch_out more than 24 hours after punch_in")

    # Recompute derived fields (R2-R5).
    if new_status in PRESENCE_STATUSES:
        late = compute_late_minutes(new_in_ms, emp["shift_start"], att_date) if new_in_ms else 0
        if new_out_ms is not None:
            work_hours = compute_work_hours(new_in_ms, new_out_ms)
            overtime = compute_overtime(new_out_ms, emp["shift_start"], emp["shift_end"], att_date)
            half_day = compute_half_day(work_hours)
        else:
            work_hours, overtime, half_day = None, 0, False
    else:
        late, work_hours, overtime, half_day = 0, None, 0, False

    new_fields = {
        "status": new_status,
        "punch_in": ms_to_dt(new_in_ms) if new_in_ms is not None else None,
        "punch_out": ms_to_dt(new_out_ms) if new_out_ms is not None else None,
        "work_hours": work_hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "half_day": half_day,
    }

    # Build the change-set of only fields that actually changed.
    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": int(rec.get("late_minutes") or 0),
        "overtime_minutes": int(rec.get("overtime_minutes") or 0),
        "half_day": bool(rec.get("half_day") or False),
    }
    changes = {}
    for k in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        if old[k] != new_fields[k]:
            changes[k] = {"from": old[k], "to": new_fields[k]}
    if not changes:
        raise HTTPException(422, "nothing to change")

    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency: only apply if the doc is still as we read it.
    # Legacy docs may have no `history` field at all; matching `{"history": []}`
    # would miss them and cause a spurious 409. Match existence accordingly.
    if "history" in rec:
        _concurrency_filter = {"_id": rec["_id"], "history": rec["history"]}
    else:
        _concurrency_filter = {"_id": rec["_id"], "history": {"$exists": False}}
    res = logs.find_one_and_update(
        _concurrency_filter,
        {"$set": new_fields, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if not res:
        raise HTTPException(409, "record was modified concurrently; retry")
    return serialize_attendance(res)


# --------------------------------------------------------------------------- #
# Analytics aggregation builders (read STORED derived values, never recompute)
# These builders are shared by the endpoints and the /admin/explain endpoint so
# the explained query is identical to the one the endpoint runs.
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str):
    first, last = month_bounds(month)
    first_s, last_s = first.isoformat(), last.isoformat()
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_s, "$lte": last_s}}},
        {"$addFields": {"_wd": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", list(PRESENCE_STATUSES)]},
                          {"$gte": ["$_wd", 2]}, {"$lte": ["$_wd", 6]}]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def build_department_summary_pipeline(month: str, department: Optional[str]):
    """Cross-collection summary. Starts from employees so zero-log employees still
    count toward headcount (R9). avg_work_hours is over records, not avg-of-avgs."""
    first, last = month_bounds(month)
    first_s, last_s = first.isoformat(), last.isoformat()
    match_emp = {"joined_on": {"$lte": last_s}}
    if department:
        match_emp["department"] = department

    # Per-log contributions. After $unwind (preserve), a missing log has no fields,
    # so every $cond treats it as not-present / not-late / no-work.
    is_presence = {"$in": ["$logs.status", list(PRESENCE_STATUSES)]}
    is_weekday = {"$and": [{"$gte": ["$logs._wd", 2]}, {"$lte": ["$logs._wd", 6]}]}
    present_contrib = {"$cond": [{"$and": [is_presence, is_weekday]},
                                 {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                                 0]}
    work_cond = {"$and": [is_presence, {"$ne": ["$logs.work_hours", None]}]}
    is_late = {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}

    per_emp = {"$group": {
        "_id": "$emp_code",
        "department": {"$first": "$department"},
        "present_days": {"$sum": present_contrib},
        "work_sum": {"$sum": {"$cond": [work_cond, "$logs.work_hours", 0]}},
        "work_count": {"$sum": {"$cond": [work_cond, 1, 0]}},
        "late_count": {"$sum": {"$cond": [is_late, 1, 0]}},
        "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
        "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
    }}

    per_dept = {"$group": {
        "_id": "$department",
        "headcount": {"$sum": 1},
        "present_days": {"$sum": "$present_days"},
        "work_sum": {"$sum": "$work_sum"},
        "work_count": {"$sum": "$work_count"},
        "late_count": {"$sum": "$late_count"},
        "total_late_minutes": {"$sum": "$total_late_minutes"},
        "leave_count": {"$sum": "$leave_count"},
        "on_duty_count": {"$sum": "$on_duty_count"},
    }}

    project = {"$project": {
        "_id": 0,
        "department": "$_id",
        "headcount": 1,
        "present_days": 1,
        "avg_work_hours": {"$cond": [
            {"$gt": ["$work_count", 0]},
            {"$divide": ["$work_sum", "$work_count"]},
            None,
        ]},
        "late_count": 1,
        "total_late_minutes": 1,
        "leave_count": 1,
        "on_duty_count": 1,
    }}

    return [
        {"$match": match_emp},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"ec": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$ec"]},
                    {"$gte": ["$date", first_s]},
                    {"$lte": ["$date", last_s]},
                ]}}},
                {"$addFields": {
                    "_wd": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                    "half_day": {"$ifNull": ["$half_day", False]},
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                }},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        per_emp,
        per_dept,
        project,
        {"$sort": {"department": 1}},
    ]


def build_late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]):
    first, last = month_bounds(month)
    first_s, last_s = first.isoformat(), last.isoformat()
    pipeline = [
        {"$match": {"date": {"$gte": first_s, "$lte": last_s}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},   # standard competition ranking
        }},
        {"$match": {"rank": {"$lte": limit}}},   # limit AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]
    return pipeline


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str = Path(...), month: str = Query(..., pattern=MONTH_RE)):
    emp = _get_employee_or_404(emp_code)
    first, last = month_bounds(month)
    joined = date.fromisoformat(emp["joined_on"])
    wd = working_days_count(first, last, joined)

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    rows = list(logs.aggregate(pipeline))
    r = rows[0] if rows else {}
    present_days = r.get("present_days", 0)
    attendance_pct = round_half_up(present_days / wd * 100, 2) if wd > 0 else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": present_days,
        "leave_days": int(r.get("leave_days", 0)),
        "late_count": int(r.get("late_count", 0)),
        "total_late_minutes": int(r.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(r.get("total_overtime_minutes", 0)),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_RE), department: Optional[str] = None):
    pipeline = build_department_summary_pipeline(month, department)
    items = []
    for r in employees.aggregate(pipeline):
        if r["headcount"] == 0:
            continue  # omit zero-headcount departments
        r["present_days"] = round_half_up(r["present_days"], 2)
        r["avg_work_hours"] = round_half_up(r["avg_work_hours"], 2) if r["avg_work_hours"] is not None else None
        items.append(r)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_RE),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_: str = Query(..., alias="from", pattern=DATE_RE),
    to: str = Query(..., pattern=DATE_RE),
):
    return _department_trend_impl(department, from_, to)


def build_department_trend_pipeline(department: str, from_s: str, to_s: str, emp_codes: list):
    """Daily trend fully in MongoDB: $densify gap-fill + $setWindowFields moving avg.

    - $match+$group aggregates stored present/late per day (indexed).
    - $densify generates every calendar day in [from, to] even with zero logs.
    - $lookup counts headcount per day (joined_on <= day, R9) including zero-log staff.
    - attendance_rate only on Mon-Fri with headcount>0, half-up 4-dec via
      floor(x*10000+0.5)/10000 (non-negative, avoids banker's $round).
    - moving_avg_7d = $avg over current+6 preceding rates inside range ($avg skips nulls).
    """
    from_d = date.fromisoformat(from_s)
    to_d = date.fromisoformat(to_s)
    from_dt = datetime(from_d.year, from_d.month, from_d.day, tzinfo=timezone.utc)
    to_excl = datetime(to_d.year, to_d.month, to_d.day, tzinfo=timezone.utc) + timedelta(days=1)
    return [
        {"$match": {"emp_code": {"$in": emp_codes}, "date": {"$gte": from_s, "$lte": to_s}}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [
                {"$in": ["$status", list(PRESENCE_STATUSES)]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$addFields": {"day": {"$dateFromString": {"dateString": "$_id"}}}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_excl]}}},
        {"$addFields": {
            "dateStr": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$lookup": {"from": "employees", "let": {"ds": "$dateStr"}, "pipeline": [
            {"$match": {"$expr": {"$and": [
                {"$eq": ["$department", department]},
                {"$lte": ["$joined_on", "$$ds"]},
            ]}}},
            {"$count": "c"},
        ], "as": "_hc"}},
        {"$addFields": {
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$_hc.c", 0]}, 0]},
            "_wd": {"$dayOfWeek": "$day"},
        }},
        {"$addFields": {"is_working_day": {"$and": [{"$gte": ["$_wd", 2]}, {"$lte": ["$_wd", 6]}]}}},
        {"$addFields": {"attendance_rate_raw": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"day": 1},
                              "output": {"moving_avg_raw": {"$avg": "$attendance_rate_raw",
                                                            "window": {"documents": [-6, 0]}}}}},
        {"$addFields": {
            "attendance_rate": {"$cond": [{"$eq": ["$attendance_rate_raw", None]}, None,
                {"$divide": [{"$floor": {"$add": [{"$multiply": ["$attendance_rate_raw", 10000]}, 0.5]}}, 10000]}]},
            "moving_avg_7d": {"$cond": [{"$eq": ["$moving_avg_raw", None]}, None,
                {"$divide": [{"$floor": {"$add": [{"$multiply": ["$moving_avg_raw", 10000]}, 0.5]}}, 10000]}]},
        }},
        {"$sort": {"day": 1}},
        {"$project": {"_id": 0, "date": "$dateStr", "is_working_day": 1, "headcount": 1,
                      "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
    ]


def _department_trend_impl(department: str, from_s: str, to_s: str):
    try:
        from_d, to_d = date.fromisoformat(from_s), date.fromisoformat(to_s)
    except ValueError:
        raise HTTPException(422, "invalid date, expected YYYY-MM-DD")
    if to_d < from_d:
        raise HTTPException(422, "to must be >= from")
    if (to_d - from_d).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")

    # 404 unknown department (no employees).
    if employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "unknown department")

    emp_codes = [e["emp_code"] for e in employees.find(
        {"department": department, "joined_on": {"$lte": to_s}}, {"emp_code": 1})]
    pipeline = build_department_trend_pipeline(department, from_s, to_s, emp_codes)
    items = list(logs.aggregate(pipeline))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# GET /admin/explain/{endpoint}
# Runs explain(executionStats) on the same query/pipeline the endpoint builds.
# --------------------------------------------------------------------------- #
EXPLAIN_ENDPOINTS = ("attendance_list", "employee_monthly", "department_summary",
                     "late_leaderboard", "department_trend")


def _explain_json(explain_doc) -> dict:
    """Convert a raw explain document (with BSON types) to plain JSON.

    json_util.dumps renders BSON types (Timestamp, ObjectId, Date, ...) as
    Extended JSON; plain json.loads keeps them as dicts/strings so FastAPI's
    jsonable_encoder can serialize them. Using json_util.loads here would
    round-trip back to BSON types (e.g. Timestamp) and cause a 500.
    """
    return json.loads(json_util.dumps(explain_doc))


def _run_explain(cmd) -> dict:
    return _explain_json(db.command("explain", cmd, verbosity="executionStats"))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from", pattern=DATE_RE),
    to: Optional[str] = Query(None, pattern=DATE_RE),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint not in EXPLAIN_ENDPOINTS:
        raise HTTPException(422, f"endpoint must be one of {EXPLAIN_ENDPOINTS}")

    if endpoint == "attendance_list":
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _run_explain(cmd)}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        cmd = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _run_explain(cmd)}

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        pipeline = build_department_summary_pipeline(month, department)
        cmd = {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}
        return {"endpoint": endpoint, "collection": "employees", "explain": _run_explain(cmd)}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        cmd = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _run_explain(cmd)}

    if endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(422, "department, from and to are required")
        try:
            _fd, _td = date.fromisoformat(from_), date.fromisoformat(to)
        except ValueError:
            raise HTTPException(422, "invalid date, expected YYYY-MM-DD")
        # Explain the exact pipeline the endpoint runs (densify + window).
        emp_codes = [e["emp_code"] for e in employees.find(
            {"department": department, "joined_on": {"$lte": to}}, {"emp_code": 1})]
        pipeline = build_department_trend_pipeline(department, from_, to, emp_codes)
        cmd = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _run_explain(cmd)}
