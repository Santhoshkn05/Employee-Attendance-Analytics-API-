"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from bson.decimal128 import Decimal128
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError



load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

db.employees.create_index([("emp_code", 1)], unique=True)
db.employees.create_index([("department", 1), ("joined_on", 1)])
db.employees.create_index([("joined_on", 1)])
db.attendance_logs.create_index(
    [("emp_code", 1), ("date", 1)], unique=True
)
db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
db.attendance_logs.create_index([("date", 1), ("late_minutes", 1), ("emp_code", 1)])

IST = timezone(timedelta(hours=5, minutes=30))


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    servers=[
        {"url": "http://127.0.0.1:8000"}
    ],
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def parse_calendar_month(value: str) -> tuple[int, int]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise HTTPException(status_code=422, detail="month must use YYYY-MM format")
    year, month = map(int, value.split("-"))
    return year, month


def calendar_month_bounds(value: str) -> tuple[int, int, date, date]:
    year, month = parse_calendar_month(value)
    start = date(year, month, 1)
    if month == 12 and year == 9999:
        end = date.max
    elif month == 12:
        end = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(year, month + 1, 1) - timedelta(days=1)
    return year, month, start, end


def compute_late_minutes(
        punch_in: datetime,
        shift_start: str,
        attendance_date: str,
) -> int:
    local_punch_in = punch_in.astimezone(IST)
    shift_date = datetime.fromisoformat(attendance_date).date()
    h, m = map(int, shift_start.split(":"))

    shift_start_dt = datetime(
        shift_date.year,
        shift_date.month,
        shift_date.day,
        h,
        m,
        tzinfo=IST,
    )
    elapsed_seconds = int(
        (local_punch_in - shift_start_dt). total_seconds()
    )

    if elapsed_seconds <= 10 * 60:
        return 0
    return elapsed_seconds // 60


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    elapsed_seconds = int((punch_out - punch_in).total_seconds())
    hours = Decimal(elapsed_seconds) / Decimal(3600)
    return float(
        hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )


def compute_overtime(
        punch_out: datetime,
        shift_end: str,
        attendance_date: str,
        shift_start: str,
) -> int:
        start_hour, start_minute = map(int, shift_start.split(":"))
        end_hour, end_minute = map(int, shift_end.split(":"))

        start_minutes = start_hour * 60 + start_minute
        end_minutes = end_hour * 60 + end_minute

        end_date = datetime.fromisoformat(attendance_date).date()

        if end_minutes <= start_minutes:
            end_date += timedelta(days=1)

        shift_end_dt = datetime(
            end_date.year,
            end_date.month,
            end_date.day,
            end_hour,
            end_minute,
            tzinfo=IST,
        )
        local_punch_out = punch_out.astimezone(IST)
        elapsed_seconds = int(
            (local_punch_out - shift_end_dt).total_seconds()
    )

        if elapsed_seconds < 30 * 60:
            return 0
        return elapsed_seconds // 60

def epoch_millis(value):
    if value is None:
        return None
    return int(value.timestamp() * 1000)


def attendance_api_record(record: dict) -> dict:
    result = dict(record)
    result.pop("_id", None)
    result.pop("id", None)
    for field in ("punch_in", "punch_out"):
        if field in result:
            result[field] = epoch_millis(result[field])
    result["history"] = [dict(entry) for entry in result.get("history", [])]
    for entry in result["history"]:
        if "at" in entry:
            entry["at"] = epoch_millis(entry["at"])
        entry["changes"] = {field: dict(change) for field, change in entry.get("changes", {}).items()}
        for field in ("punch_in", "punch_out"):
            if field in entry["changes"]:
                for direction in ("from", "to"):
                    if direction in entry["changes"][field]:
                        entry["changes"][field][direction] = epoch_millis(entry["changes"][field][direction])
    return result


def explain_json_safe(value):
    """Convert BSON Decimal128 values in explain plans to JSON encodable decimals."""
    if isinstance(value, Decimal128):
        return value.to_decimal()
    if isinstance(value, dict):
        return {key: explain_json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [explain_json_safe(item) for item in value]
    return value


def get_attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    start_hour, start_minute = map(int, shift_start.split(":"))
    end_hour, end_minute = map(int, shift_end.split(":"))

    start_minutes = start_hour * 60 + start_minute
    end_minutes = end_hour * 60 + end_minute
    punch_minutes = punch_in.hour * 60 + punch_in.minute

    is_overnight = end_minutes <= start_minutes

    if is_overnight and punch_minutes < end_minutes:
        return (punch_in.date() - timedelta(days=1)).isoformat()

    return punch_in.date().isoformat()

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=1, max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Invalid email format")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, value:str) -> str:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("Time must use HH:MM format")

        return value

    @field_validator("shift_end")
    @classmethod
    def validate_shift_difference(cls, value: str, info) -> str:
        if info.data.get("shift_start") == value:
            raise ValueError("Shift start and end must differ")
        return value

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value:str) -> str:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("joined on must use YYYY-MM-DD format")
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError("joined on must be a valid date")
        return value

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True, ge=100000000000, le=4102444800000)

class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(default=None, strict=True, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(default=None, strict=True, ge=100000000000, le=4102444800000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def validate_allowed_fields(cls, value):
        if isinstance(value, dict):
            allowed_fields = {"status", "punch_in", "punch_out", "reason", "regularized_by"}
            derived_fields = {"work_hours", "late_minutes", "overtime_minutes", "half_day"}
            unexpected_fields = set(value) - allowed_fields - derived_fields
            if unexpected_fields:
                raise ValueError("Only status, punch_in, punch_out, reason, and regularized_by are allowed")
            return {key: item for key, item in value.items() if key not in derived_fields}
        return value

    @field_validator("status", "punch_in", "punch_out", mode="before")
    @classmethod
    def validate_change_value(cls, value):
        if value is None:
            raise ValueError("Change fields cannot be null")
        return value

# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="MongoDB unavailable",
        )

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists",
        )
    doc.pop("_id", None)
    doc["created_at"] = epoch_millis(doc["created_at"])
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100)
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)

    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )
    for employee in items:
        if employee.get("created_at") is not None:
            employee["created_at"] = epoch_millis(employee["created_at"])
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(
            status_code=404,
            detail = "Employee not found"
        )
    if body.punched_at is not None:
        ts = datetime.fromtimestamp(
            body.punched_at / 1000,
            tz = timezone.utc
        ).astimezone(IST)
    else:
        ts = datetime.now(IST)
    ts = ts.replace(microsecond=0)
    d = get_attendance_date(
        ts,
        emp["shift_start"],
        emp["shift_end"],
    )

    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            ts,
            emp["shift_start"],
            d,
    ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        res = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="already punched in for this date",
        )

    return attendance_api_record(doc)

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge = 1),
    page_size: int = Query(default=20, ge=1, le= 100),
):
    for label, value in (("date_from", date_from), ("date_to", date_to)):
        if value is not None:
            try:
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError
            except ValueError:
                raise HTTPException(status_code=422, detail=f"{label} must use YYYY-MM-DD format")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must be on or before date_to",
        )
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
    total = db.attendance_logs.count_documents(q)

    docs = list(
        db.attendance_logs.find(q, {"_id": 0, "id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    docs = [attendance_api_record(doc) for doc in docs]

    return {
        "items": docs,
        "total": total,
        "page": page,
        "page_size": page_size,
    }

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(
            status_code= 404,
            detail = "Employee not found",
        )
    if body.punched_at is not None:
        ts= datetime.fromtimestamp(
            body.punched_at / 1000,
            tz = timezone.utc,
        ).replace(microsecond=0)
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)

    record = db.attendance_logs.find_one(
        {
        "emp_code": body.emp_code,
        "punch_out": None,
        "punch_in": {"$ne": None, "$lte": ts},
    },
    sort=[("punch_in", -1)]
)

    if record is None:
        already_closed = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$ne": None, "$lte": ts},
                "punch_out": {"$ne": None},
            },
            sort=[("punch_in", -1)],
        )
        if already_closed is not None:
            raise HTTPException(status_code=409, detail="Attendance record has already been punched out")
        future_open = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$gt": ts}, "punch_out": None},
            sort=[("punch_in", -1)],
        )
        if future_open is not None:
            raise HTTPException(status_code=422, detail="Punch-out time must be after punch-in time and within 24 hours")
        raise HTTPException(status_code=404, detail="No open punch-in record found")

    punch_in_time = record["punch_in"]
    elapsed_seconds = int((ts - punch_in_time).total_seconds())

    if elapsed_seconds <= 0 or elapsed_seconds > 24 * 60 * 60:
        raise HTTPException(
            status_code=422,
            detail="Punch-out time must be after punch-in time and within 24 hours",
        )

    work_hours = compute_work_hours(punch_in_time, ts)
    overtime_minutes = compute_overtime(
        ts,
        emp["shift_end"],
        record["date"],
        emp["shift_start"],
    )
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": work_hours < 4.50,
            },
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance record has already been punched out",
        )

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return attendance_api_record(updated)

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeIn,
):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise HTTPException(status_code=422, detail="date must use YYYY-MM-DD format")
    try:
        if datetime.fromisoformat(date).date().isoformat() != date:
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail="date must be a valid calendar date")

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(
            status_code=404,
            detail = "Employee not found",
        )
    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date,
    })

    if record is None:
        raise HTTPException(
            status_code=404,
            detail= "Attendance record not found",
        )
    requested_changes = body.model_dump(
        exclude_unset=True,
        exclude={"reason", "regularized_by"},
    )

    if not requested_changes:
        raise HTTPException(
            status_code=422,
            detail="At least one of status, punch_in, or punch_out must be provided",
        )

    allowed_fields = {"status", "punch_in", "punch_out"}
    if not set(requested_changes).issubset(allowed_fields):
        raise HTTPException(
            status_code=422,
            detail="Only status, punch_in, and punch_out can be changed",
        )

    def to_datetime(epoch_millis: int) -> datetime:
        return datetime.fromtimestamp(
            epoch_millis / 1000,
            tz=timezone.utc,
        ).replace(microsecond=0)

    def normalize_datetime(value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(microsecond=0)

    status = requested_changes.get("status", record["status"])
    if status not in {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}:
        raise HTTPException(
            status_code=422,
            detail="Invalid attendance status",
        )

    requested_punch_fields = {"punch_in", "punch_out"} & set(requested_changes)
    is_non_presence = status in {"ABSENT", "LEAVE"}
    if is_non_presence and requested_punch_fields:
        raise HTTPException(
            status_code=422,
            detail="punch_in and punch_out cannot be supplied with ABSENT or LEAVE",
        )

    if is_non_presence:
        punch_in = None
        punch_out = None
        late_minutes = 0
        work_hours = None
        overtime_minutes = 0
        half_day = False
    else:
        if "punch_in" in requested_changes:
            punch_in = to_datetime(requested_changes["punch_in"])
        else:
            punch_in = normalize_datetime(record.get("punch_in"))
        if punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="A presence status requires punch_in",
            )

        if get_attendance_date(
            punch_in.astimezone(IST),
            emp["shift_start"],
            emp["shift_end"],
        ) != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must belong to the record attendance date",
            )

        if "punch_out" in requested_changes:
            punch_out = to_datetime(requested_changes["punch_out"])
        else:
            punch_out = normalize_datetime(record.get("punch_out"))

        if punch_out is not None:
            elapsed_seconds = int((punch_out - punch_in).total_seconds())
            if elapsed_seconds <= 0 or elapsed_seconds > 24 * 60 * 60:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be after punch_in and within 24 hours",
                )
            work_hours = compute_work_hours(punch_in, punch_out)
            overtime_minutes = compute_overtime(
                punch_out,
                emp["shift_end"],
                date,
                emp["shift_start"],
            )
            half_day = work_hours < 4.50
        else:
            work_hours = None
            overtime_minutes = 0
            half_day = False

        late_minutes = compute_late_minutes(
            punch_in,
            emp["shift_start"],
            date,
        )

    final_values = {
        "status": status,
        "punch_in": punch_in,
        "punch_out": punch_out,
        "late_minutes": late_minutes,
        "work_hours": work_hours,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }
    default_values = {
        "punch_in": None,
        "punch_out": None,
        "late_minutes": 0,
        "work_hours": None,
        "overtime_minutes": 0,
        "half_day": False,
    }
    changes = {}
    for field, new_value in final_values.items():
        old_value = record.get(field, default_values.get(field))
        if old_value != new_value:
            changes[field] = {"from": old_value, "to": new_value}

    if not changes:
        raise HTTPException(
            status_code=422,
            detail="The request does not change the attendance record",
        )

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    expected_record = {"_id": record["_id"]}
    for field in ("status", "punch_in", "punch_out", "history"):
        expected_record[field] = (
            record[field] if field in record else {"$exists": False}
        )

    updated = db.attendance_logs.find_one_and_update(
        expected_record,
        {
            "$set": final_values,
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(
            status_code=409,
            detail="Attendance record changed concurrently; retry the request",
        )

    return attendance_api_record(updated)

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    year, month_number, month_start, month_end = calendar_month_bounds(month)
    employee = db.employees.find_one(
        {"emp_code": emp_code},
        {"_id": 0, "emp_code": 1, "joined_on": 1},
    )

    if employee is None:
        raise HTTPException(status_code= 404, detail="Employee not found")

    joined_on = date.fromisoformat(employee["joined_on"])
    effective_start = max(month_start, joined_on)
    working_days = 0
    current_day = effective_start

    while current_day <= month_end:
        if current_day.weekday() < 5:
            working_days += 1
        current_day += timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": max(month_start, joined_on).isoformat(),
                    "$lte": month_end.isoformat(),
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                              "$and": [
                                  {
                                      "$in": [
                                      "$status",
                                      ["PRESENT", "WFH", "ON_DUTY"],
                                  ]
                                  },
                                  {
                                      "$in": [
                                          {
                                              "$dayOfWeek": {
                                                  "$dateFromString": {
                                                      "dateString": "$date"
                                                  }
                                              }
                                          },
                                          [2,3,4,5,6],
                                      ]
                                  },
                              ]
                              },
                              {
                                        "$cond": [
                                            {"$eq": ["$half_day", True]},
                                            0.5,
                                            1,
                                        ]
                                    },
                                    0,
                                    ]
                                }
                            },
                            "leave_days": {
                                "$sum" : {
                                    "$cond": [
                                        {"$eq": ["$status", "LEAVE"]},
                                        1,
                                        0,
                                    ]
                                }
                                },

                                "late_count": {
                                    "$sum": {
                                        "$cond": [
                                            {"$gt": ["$late_minutes", 0]},
                                            1,
                                            0,
                                        ]
                                    }
                                },
                                "total_late_minutes" : {
                                    "$sum": "$late_minutes"
                                },
                                "total_overtime_minutes": {
                                    "$sum": "$overtime_minutes"
                                },
                            }
                        },
                    ]
    result = list(db.attendance_logs.aggregate(pipeline))
    stats = result[0] if result else {
        "present_days": 0,
        "leave_days": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "total_overtime_minutes": 0,
    }
    if working_days > 0:
        attendance_pct = float(
            (
                Decimal(str(stats["present_days"]))
                / Decimal(working_days)
                * Decimal("100")
            ).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        )
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": stats["present_days"],
        "leave_days": stats["leave_days"],
        "late_count": stats["late_count"],
        "total_late_minutes": stats["total_late_minutes"],
        "total_overtime_minutes": stats["total_overtime_minutes"],
        "attendance_pct": attendance_pct,
    }

def build_department_summary_pipeline(month_start: date, month_end: date, department: Optional[str]):
    employee_match = {"joined_on": {"$lte": month_end.isoformat()}}
    if department is not None:
        employee_match["department"] = department
    pipeline = [
        {"$match": employee_match},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_codes": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$in": ["$emp_code", "$$employee_codes"]
                            },
                            "date": {
                                "$gte": month_start.isoformat(),
                                "$lte": month_end.isoformat(),
                            },
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]},
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {
                                                    "$eq": ["$half_day", True]
                                                },
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "work_hours_sum": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                "$in": [
                                                    "$status",
                                                    ["PRESENT", "WFH", "ON_DUTY"],
                                                ]
                                                },
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        "$work_hours",
                                        0,
                                    ]
                                }
                            },
                            "work_hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        ["PRESENT", "WFH", "ON_DUTY"],
                                                    ]
                                                },
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": ["$late_minutes", 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "total_late_minutes": {
                                "$sum": "$late_minutes"
                            },
                            "leave_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$status", "LEAVE"]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "on_duty_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$status", "ON_DUTY"]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "attendance_stats",
            }
        },
        {
            "$addFields": {
                "stats": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$attendance_stats", 0]},
                        {
                            "present_days": 0,
                            "work_hours_sum": 0,
                            "work_hours_count": 0,
                            "late_count": 0,
                            "total_late_minutes": 0,
                            "leave_count": 0,
                            "on_duty_count": 0,
                        },
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": "$stats.present_days",
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$stats.work_hours_count", 0]},
                        {
                            "$divide": [
                                {"$floor": {"$add": [{"$multiply": [{"$divide": ["$stats.work_hours_sum", "$stats.work_hours_count"]}, 100]}, 0.5]}},
                                100,
                            ]
                        },
                        None,
                    ]
                },
                "late_count": "$stats.late_count",
                "total_late_minutes": "$stats.total_late_minutes",
                "leave_count": "$stats.leave_count",
                "on_duty_count": "$stats.on_duty_count",
            }
        },
        {"$sort": {"department": 1}},
    ]
    return pipeline


@app.get("/analytics/departments/summary")
def department_summary_analytics(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    year, month_number, month_start, month_end = calendar_month_bounds(month)
    employee_match = {
        "joined_on": {"$lte": month_end.isoformat()}
    }
    if department is not None:
        employee_match["department"] = department
    pipeline = build_department_summary_pipeline(month_start, month_end, department)
    departments = list(db.employees.aggregate(pipeline))
    return {"month": month, "items": departments}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    year, month_number, month_start, month_end = calendar_month_bounds(month)

    pipeline = [
        {
            "$match" : {
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lte": month_end.isoformat(),
                },
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
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append(
            {"$match": {"employee.department": department}}
        )
    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    leaderboard = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": leaderboard}


def build_department_trend_pipeline(department: str, from_date: date, to_date: date):
    pipeline = [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": None,
                "employees": {
                    "$push": {
                        "emp_code": "$emp_code",
                        "joined_on": "$joined_on",
                    }
                },
            }
        },
        {
            "$set": {
                "start_date": {
                    "$dateFromString": {"dateString": from_date.isoformat()}
                },
                "end_date": {
                    "$dateFromString": {"dateString": to_date.isoformat()}
                },
            }
        },
        {
            "$set": {
                "dates": {
                    "$map": {
                        "input": {
                            "$range": [
                                0,
                                {
                                    "$add": [
                                        {
                                            "$dateDiff": {
                                                "startDate": "$start_date",
                                                "endDate": "$end_date",
                                                "unit": "day",
                                            }
                                        },
                                        1,
                                    ]
                                },
                            ]
                        },
                        "as": "day_offset",
                        "in": {
                            "$dateToString": {
                                "date": {
                                    "$dateAdd": {
                                        "startDate": "$start_date",
                                        "unit": "day",
                                        "amount": "$$day_offset",
                                    }
                                },
                                "format": "%Y-%m-%d",
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$dates"},
        {
            "$set": {
                "date_value": {
                    "$dateFromString": {"dateString": "$dates"}
                },
                "eligible_employees": {
                    "$filter": {
                        "input": "$employees",
                        "as": "employee",
                        "cond": {"$lte": ["$$employee.joined_on", "$dates"]},
                    }
                },
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "date": "$dates",
                    "employee_codes": {
                        "$map": {
                            "input": "$eligible_employees",
                            "as": "employee",
                            "in": "$$employee.emp_code",
                        }
                    },
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$date"]},
                                    {"$in": ["$emp_code", "$$employee_codes"]},
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$in": [
                                                "$status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": ["$half_day", True]},
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$gt": [
                                                {"$ifNull": ["$late_minutes", 0]},
                                                0,
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "daily_counts",
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$dates",
                "headcount": {"$size": "$eligible_employees"},
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$date_value"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$date_value"}, 6]},
                    ]
                },
                "present_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$daily_counts.present_count", 0]},
                        0,
                    ]
                },
                "late_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$daily_counts.late_count", 0]},
                        0,
                    ]
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$or": [
                                {"$eq": ["$is_working_day", False]},
                                {"$eq": ["$headcount", 0]},
                            ]
                        },
                        None,
                        {
                            "$let": {
                                "vars": {
                                    "rate": {
                                        "$multiply": [
                                            {
                                                "$divide": [
                                                    {"$toDecimal": "$present_count"},
                                                    {"$toDecimal": "$headcount"},
                                                ]
                                            },
                                            {"$toDecimal": 100},
                                        ]
                                    }
                                },
                                "in": {
                                    "$toDouble": {
                                        "$divide": [
                                            {
                                                "$floor": {
                                                    "$add": [
                                                        {
                                                            "$multiply": [
                                                                "$$rate",
                                                                {"$toDecimal": 10000},
                                                            ]
                                                        },
                                                        {"$toDecimal": "0.5"},
                                                    ]
                                                }
                                            },
                                            {"$toDecimal": 10000},
                                        ]
                                    }
                                },
                            }
                        },
                    ]
                }
            }
        },
        {
            "$setWindowFields" : {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        }
                    }
                }
            }
        },
        {
            "$set": {
                "moving_avg_7d" : {
                    "$cond": [
                        {"$eq": ["$moving_avg_7d", None]},
                        None,
                        {
                            "$divide": [
                                {"$floor": {"$add": [{"$multiply": ["$moving_avg_7d", 10000]}, 0.5]}},
                                10000,
                            ]
                        },
                    ]
                }
            }
        },
        {"$sort": {"date": 1}},
    ]
    return pipeline

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: date = Query(..., alias = "from"),
    to_date: date = Query(..., alias= "to"),
):
    if from_date > to_date:
        raise HTTPException(
            status_code=422,
            detail="'from' date cannot be after 'to' date",
        )
    if (to_date - from_date).days + 1 > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range cannot exceed 92 calender days",
        )
    employee_exists = db.employees.find_one(
        {"department": department},
        {"_id": 1},
    )
    if employee_exists is None:
        raise HTTPException(
            status_code=404,
            detail=f"Department '{department}' not found",
        )
    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    trend = list(db.employees.aggregate(pipeline))
    return {"department": department, "items": trend}

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int= Query(default=20, ge=1, le=100),
    limit: int = Query(default=10, ge=1, le=50),
):
    year = month_number = None
    if month is not None:
        year, month_number = parse_calendar_month(month)

    if endpoint == "attendance_list":
        for label, value in (("date_from", date_from), ("date_to", date_to)):
            if value is not None:
                try:
                    if date.fromisoformat(value).isoformat() != value:
                        raise ValueError
                except ValueError:
                    raise HTTPException(status_code=422, detail=f"{label} must use YYYY-MM-DD format")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(
                status_code=422,
                detail="date_from must be on or before date_to",
            )
        query = {}
        if emp_code:
            query["emp_code"] = emp_code

        if date_from or date_to:
            query["date"] = {}

            if date_from:
                query["date"]["$gte"] = date_from

            if date_to:
                query["date"]["$lte"] = date_to

        if status:
            query["status"] = status

        result = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page-1) * page_size,
                "limit": page_size,
            },
            verbosity = "executionStats",
            )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_json_safe(result)}

    if endpoint == "employee_monthly":
        if emp_code is None or year is None or month_number is None:
            raise HTTPException(
                status_code=422,
                detail = "emp_code year, and month are required for employee_monthly",
            )
        employee=db.employees.find_one(
            {"emp_code": emp_code},
            {"_id": 0, "emp_code": 1, "joined_on": 1},
        )
        if employee is None:
            raise HTTPException(
                status_code=404,
                detail="Employee not found",
            )
        _, _, month_start, month_end = calendar_month_bounds(month)
        joined_on = date.fromisoformat(employee["joined_on"])
        effective_start = max(month_start, joined_on)

        pipeline = [
            {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": effective_start.isoformat(),
                    "$lte": month_end.isoformat(),
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date"
                                                    }
                                                }
                                            },
                                            [2,3,4,5,6],
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
        ]
        result = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor" : {},
            },
            verbosity = "executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_json_safe(result)}


    if endpoint == "department_summary":
        if year is None or month_number is None:
            raise HTTPException(
                status_code=422,
                detail="year and month are required for department_summary",
            )

        _, _, month_start, month_end = calendar_month_bounds(month)

        pipeline = build_department_summary_pipeline(month_start, month_end, department)

        result = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        return {"endpoint": endpoint, "collection": "employees", "explain": explain_json_safe(result)}

    if endpoint == "late_leaderboard":
        if year is None or month_number is None:
            raise HTTPException(
                status_code=422,
                detail="year and month are required for late_leaderboard",
            )
        _, _, month_start, month_end = calendar_month_bounds(month)

        pipeline = [
            {
                "$match": {
                    "date": {
                        "$gte": month_start.isoformat(),
                        "$lte": month_end.isoformat(),
                    },
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
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
        ]
        if department is not None:
            pipeline.append(
                {"$match": {"employee.department": department}}
            )
        pipeline.extend([
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            # Match the public pipeline exactly; this is implied by the first
            # late_minutes filter but remains part of the endpoint query.
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {"$match": {"rank": {"$lte": limit}}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ])
        result = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_json_safe(result)}

    if endpoint == "department_trend":
        if department is None or from_date is None or to_date is None:
            raise HTTPException(status_code=422, detail="department, from and to are required for department_trend")
        try:
            trend_from = date.fromisoformat(from_date)
            trend_to = date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="Dates must use YYYY-MM-DD format")
        if trend_from.isoformat() != from_date or trend_to.isoformat() != to_date:
            raise HTTPException(status_code=422, detail="Dates must use YYYY-MM-DD format")
        if trend_from > trend_to:
            raise HTTPException(status_code=422, detail="'from' date cannot be after 'to' date")
        if (trend_to - trend_from).days + 1 > 92:
            raise HTTPException(status_code=422, detail="Date range cannot exceed 92 calendar days")
        if db.employees.find_one({"department": department}, {"_id": 1}) is None:
            raise HTTPException(status_code=404, detail=f"Department '{department}' not found")
        pipeline = build_department_trend_pipeline(department, trend_from, trend_to)
        result = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_json_safe(result)}

    raise HTTPException(
        status_code=400,
        detail=f"Explain not implemented for endpoint: {endpoint}",
    )
