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
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError



load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

db.employees.create_index("emp_code", unique=True)
db.attendance_logs.create_index(
    [("emp_code", 1), ("date", 1)],
    unique=True,
)

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
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

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
    
    doc.pop("_id", None)
    for field in ("punch_in", "punch_out"):
        value = doc.get(field)
        if value is not None:
            doc[field] = int(value.timestamp() * 1000)
    return doc

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge = 1),
    page_size: int = Query(default=20, ge=1, le= 100),
):
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

    for doc in docs:
        for field in ("punch_in", "punch_out"):
            value = doc.get(field)
            if value is not None:
                doc[field] = int(value.timestamp() * 1000)

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
        raise HTTPException(
            status_code=404,
            detail="No open punch-in record found",
        )

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
    updated.pop("_id", None)
    updated.pop("id", None)

    for field in ("punch_in", "punch_out"):
        value = updated.get(field)
        if value is not None:
            updated[field] = int(value.timestamp() * 1000)
    return updated

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

    updated.pop("_id", None)
    updated.pop("id", None)
    for field in ("punch_in", "punch_out"):
        value = updated.get(field)
        if value is not None:
            updated[field] = int(value.timestamp() * 1000)
    for entry in updated.get("history", []):
        entry["at"] = int(entry["at"].timestamp() * 1000)
        for field in ("punch_in", "punch_out"):
            if field in entry.get("changes", {}):
                for direction in ("from", "to"):
                    value = entry["changes"][field].get(direction)
                    if value is not None:
                        entry["changes"][field][direction] = int(
                            value.timestamp() * 1000
                        )
    return updated

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(
    emp_code: str,
    year: int = Query(..., ge=1, le=9999),
    month: int = Query(..., ge=1, le=12),
):
    employee = db.employees.find_one(
        {"emp_code": emp_code},
        {"_id": 0, "emp_code": 1, "joined_on": 1},
    )

    if employee is None:
        raise HTTPException(status_code= 404, detail="Employee not found")
    month_start = date(year, month, 1)
    if month == 12:
        next_month_start = date(year+1, 1, 1)
    else:
        next_month_start = date(year, month +1, 1)
    month_end = next_month_start - timedelta(days=1)

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
        "year": year,
        "month": month,
        "working_days": working_days,
        "present_days": stats["present_days"],
        "leave_days": stats["leave_days"],
        "late_count": stats["late_count"],
        "total_late_minutes": stats["total_late_minutes"],
        "total_overtime_minutes": stats["total_overtime_minutes"],
        "attendance_pct": attendance_pct,
    }

@app.get("/analytics/departments/summary")
def department_summary_analytics(
    year: int = Query(..., ge=1, le=9999),
    month: int = Query(..., ge=1, le=12),
    department: Optional[str] = Query(None),
):
    month_start = date(year, month, 1)
    if month == 12:
        next_month_start = date(year+1, 1, 1)
    else:
        next_month_start = date(year, month+1, 1)
    month_end = next_month_start - timedelta(days=1)
    employee_match = {
        "joined_on": {"$lte": month_end.isoformat()}
    }
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
                                            "$in": [
                                                "$status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
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
                                "$stats.work_hours_sum",
                                "$stats.work_hours_count",
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
    departments = list(db.employees.aggregate(pipeline))
    return {
        "year": year,
        "month": month,
        "departments": departments,
    }

@app.get("/analytics/late-leaderboard")
def late_leaderboard(
    year: int = Query(..., ge=1, le=9999),
    month: int = Query(..., ge=1, le=12),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    if month == 12:
        next_month_start = date(year + 1, 1, 1)
    else:
        next_month_start = date(year, month+1, 1)

    month_start = date(year, month, 1)
    month_end = next_month_start - timedelta(days=1 )

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
            }
        },
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    leaderboard = list(db.attendance_logs.aggregate(pipeline))
    return {
        "year": year,
        "month": month,
        "leaderboard": leaderboard,
    }

# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
